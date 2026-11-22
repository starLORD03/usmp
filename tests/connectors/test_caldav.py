import hashlib
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from caldav.lib import error as caldav_error

from usmp.connectors.caldav import CalDavConnector, _errors
from usmp.connectors.ics import IcsConnector
from usmp.core.engine import migrate
from usmp.core.models import Item, ItemStatus
from usmp.domains.calendar import calendar_item, event_item
from usmp.sdk.connector import ConnectorError
from usmp.testing import ConnectorConformance

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ics"
PASSWORD_ENV = "USMP_TEST_CALDAV_PASSWORD"  # noqa: S105 - a variable name


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def radicale(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    base = tmp_path_factory.mktemp("radicale")
    store, log = base / "store", base / "radicale.log"
    port = _free_port()
    with log.open("wb") as log_file:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            [
                sys.executable,
                "-m",
                "radicale",
                "--storage-filesystem-folder",
                str(store),
                "--auth-type",
                "none",
                "--rights-type",
                "authenticated",
                "--server-hosts",
                f"127.0.0.1:{port}",
                "--logging-level",
                "info",
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + 60
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            if time.monotonic() > deadline or proc.poll() is not None:
                proc.kill()
                pytest.fail(f"Radicale did not start:\n{log.read_text(errors='replace')[-3000:]}")
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/", store
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "test")


def connector(url: str, user: str, **extra: str) -> CalDavConnector:
    return CalDavConnector({"url": url, "username": user, "password_env": PASSWORD_ENV, **extra})


def new_user() -> str:
    return f"user-{uuid.uuid4().hex[:8]}"


def sample() -> list[Item]:
    cal = calendar_item("seed", "c", {"name": "Work", "color": "#33b679", "description": "Team"})
    master = {
        "uid": "sync@x",
        "title": "Sync",
        "start": "2026-05-04T09:00:00",
        "end": "2026-05-04T10:00:00",
        "time_zone": "Europe/Paris",
        "recurrence_rules": ["FREQ=WEEKLY"],
        "excluded_dates": ["2026-05-11T09:00:00"],
        "attendees": [{"email": "a@x.com", "status": "accepted"}],
        "alarms": [{"trigger": "-PT10M"}],
    }
    moved = {
        "uid": "sync@x",
        "title": "Sync (moved)",
        "start": "2026-05-18T11:00:00",
        "time_zone": "Europe/Paris",
        "recurrence_id": "2026-05-18T09:00:00",
    }
    return [
        cal,
        event_item("seed", "e1", master, parent=cal.usmp_id),
        event_item("seed", "e2", moved, parent=cal.usmp_id),
        event_item(
            "seed",
            "e3",
            {"uid": "day@x", "start": "2026-06-01", "title": "Off"},
            parent=cal.usmp_id,
        ),
    ]


class TestCalDavConformance(ConnectorConformance):
    domain = "calendar"

    @pytest.fixture(autouse=True)
    def _server(self, radicale: tuple[str, Path]) -> None:
        self.url, self.store = radicale
        self.user = new_user()

    def make_connector(self) -> CalDavConnector:
        return connector(self.url, self.user)

    def sample_items(self) -> list[Item]:
        return sample()

    def provider_state(self) -> Any:
        root = self.store
        return {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")
            if p.is_file() and self.user in str(p) and ".Radicale.cache" not in str(p)
        }


def test_source_client_blocks_every_write_method(radicale: tuple[str, Path]) -> None:
    url, _ = radicale
    reader = connector(url, new_user()).open_reader()

    for method in ["PUT", "DELETE", "MKCALENDAR", "PROPPATCH", "MOVE", "POST", "MKCOL"]:
        with pytest.raises(ConnectorError, match=f"blocked a {method} request"):
            reader._client.request(url, method)  # type: ignore[attr-defined]


def test_ics_to_caldav_and_back_preserves_every_event(
    radicale: tuple[str, Path], tmp_path: Path
) -> None:
    url, _ = radicale
    user = new_user()

    there = migrate(
        IcsConnector({"path": str(FIXTURES / "outlook.ics")}),
        connector(url, user),
        bundle_path=tmp_path / "a.usmp",
    )
    back = migrate(
        connector(url, user),
        IcsConnector({"path": str(tmp_path / "back")}),
        bundle_path=tmp_path / "b.usmp",
    )

    assert there.report is not None and there.report.ok, there.report
    assert back.report is not None and back.report.ok, back.report

    def event_hashes(path: Path) -> dict[str, str]:
        records = IcsConnector({"path": str(path)}).open_reader().read("calendar")
        return {
            f"{r.item.payload['uid']}{r.item.payload.get('recurrence_id')}": r.item.content_hash
            for r in records
            if r.item.type == "event"
        }

    original = event_hashes(FIXTURES / "outlook.ics")
    returned = event_hashes(tmp_path / "back")
    assert returned == original


def test_name_collision_creates_unique_calendar_with_declared_loss(
    radicale: tuple[str, Path], tmp_path: Path
) -> None:
    url, _ = radicale
    user = new_user()
    target = connector(url, user)
    list(target.open_writer().write([calendar_item("s", "x", {"name": "Work"})]))

    result = migrate(
        IcsConnector({"path": str(FIXTURES / "outlook.ics")}),
        target,
        bundle_path=tmp_path / "b.usmp",
    )

    assert result.report is not None and result.report.ok, result.report
    cal = next(r for r in result.report.items if r.type == "calendar")
    assert cal.status is ItemStatus.VERIFIED_WITH_DECLARED_LOSS
    assert "name" in cal.declared_loss_fields
    assert any(loss.detail == "stored as 'Work (2)'" for loss in result.report.losses)


def test_merge_into_one_named_calendar_and_no_overwrite(
    radicale: tuple[str, Path], tmp_path: Path
) -> None:
    url, _ = radicale
    user = new_user()
    target = connector(url, user, calendar="Everything")

    first = migrate(IcsConnector({"path": str(FIXTURES)}), target, bundle_path=tmp_path / "a.usmp")
    again = migrate(IcsConnector({"path": str(FIXTURES)}), target, bundle_path=tmp_path / "b.usmp")

    assert first.report is not None and first.report.ok, first.report
    calendars = [
        r for r in connector(url, user).open_reader().read("calendar") if r.item.type == "calendar"
    ]
    assert [c.item.payload["name"] for c in calendars] == ["Everything"]
    assert again.report is not None and not again.report.ok
    assert any("already exists" in e for e in again.report.errors)


def test_read_one_calendar_by_name(radicale: tuple[str, Path]) -> None:
    url, _ = radicale
    user = new_user()
    list(
        connector(url, user)
        .open_writer()
        .write([calendar_item("s", "a", {"name": "A"}), calendar_item("s", "b", {"name": "B"})])
    )

    only_b = list(connector(url, user, calendar="B").open_reader().read("calendar"))

    assert [r.item.payload["name"] for r in only_b] == ["B"]
    with pytest.raises(ConnectorError, match="no calendar named 'C'; available: A, B"):
        list(connector(url, user, calendar="C").open_reader().read("calendar"))
    assert list(connector(url, user).open_reader().read("contacts")) == []


def test_events_without_calendar_go_to_imported(radicale: tuple[str, Path]) -> None:
    url, _ = radicale
    user = new_user()
    orphan = event_item("s", "1", {"uid": "o@x", "start": "2026-01-01"})

    [result] = list(connector(url, user).open_writer().write([orphan]))

    names = [
        r.item.payload["name"]
        for r in connector(url, user).open_reader().read("calendar")
        if r.item.type == "calendar"
    ]
    assert names == ["Imported"]
    assert result.native_id.endswith(".ics")


def test_rejects_non_calendar_items(radicale: tuple[str, Path]) -> None:
    url, _ = radicale
    note = Item.create(
        connector="s", native_id="1", domain="notes", type="note", schema_version=1, payload={}
    )
    with pytest.raises(ConnectorError, match="cannot write notes/note"):
        list(connector(url, new_user()).open_writer().write([note]))


class TestConfiguration:
    def test_missing_password_explains_how_to_store_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(PASSWORD_ENV)
        monkeypatch.setattr("usmp.sdk.credentials.get_secret", lambda *a: None)

        with pytest.raises(ConnectorError, match=r"usmp auth set caldav me@https://dav\.example"):
            connector("https://dav.example", "me").open_reader()

    def test_url_must_be_http(self) -> None:
        with pytest.raises(ConnectorError, match="must start with https://"):
            connector("ftp://dav.example", "me").open_reader()

    def test_info_label_defaults_to_username(self) -> None:
        assert connector("https://x", "me@icloud.com").info().label == "me@icloud.com"
        assert connector("https://x", "me", label="Home").info().label == "Home"


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (caldav_error.AuthorizationError("401"), "sign-in failed while testing"),
        (caldav_error.NotFoundError("404"), "not found while testing"),
        (caldav_error.DAVError("boom"), "CalDAV error while testing"),
        (ConnectionRefusedError("refused"), "CalDAV error while testing"),
    ],
)
def test_errors_are_explained(exc: Exception, message: str) -> None:
    with pytest.raises(ConnectorError, match=message), _errors("testing"):
        raise exc


def test_connector_errors_pass_through() -> None:
    with pytest.raises(ConnectorError, match=r"^original$"), _errors("testing"):
        raise ConnectorError("original")


def test_merge_mode_leaves_existing_calendar_settings_alone(radicale: tuple[str, Path]) -> None:
    url, _ = radicale
    user = new_user()
    list(
        connector(url, user)
        .open_writer()
        .write(
            [calendar_item("s", "a", {"name": "Home", "color": "#111111", "description": "Mine"})]
        )
    )

    [result] = list(
        connector(url, user, calendar="Home")
        .open_writer()
        .write(
            [calendar_item("s", "b", {"name": "Work", "color": "#222222", "description": "Theirs"})]
        )
    )

    [home] = [r.item.payload for r in connector(url, user).open_reader().read("calendar")]
    assert home == {"name": "Home", "color": "#111111", "description": "Mine"}
    assert {loss.field for loss in result.losses} == {"name", "color", "description"}
