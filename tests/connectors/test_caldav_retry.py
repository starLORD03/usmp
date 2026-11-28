"""CalDAV under trouble: rate limits, busy servers, dropped connections and parallel uploads."""

import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from caldav.davclient import DAVClient
from caldav.lib import error as caldav_error

from usmp.connectors import caldav
from usmp.connectors.caldav import CalDavConnector, _CalDavWriter
from usmp.connectors.memory import MemoryConnector
from usmp.core.engine import migrate
from usmp.domains.calendar import calendar_item, event_item
from usmp.sdk.connector import ConnectorError, WriteResult
from usmp.sdk.retry import RetryPolicy

PASSWORD_ENV = "USMP_TEST_CALDAV_PASSWORD"  # noqa: S105 - a variable name
ORIGINAL = DAVClient.request


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    monkeypatch.setenv(PASSWORD_ENV, "test")
    slept: list[float] = []
    monkeypatch.setattr(caldav._RetryingClient, "policy", RetryPolicy(sleep=slept.append))
    return slept


def connector(url: str, **extra: str) -> CalDavConnector:
    user = f"retry-{uuid.uuid4().hex[:8]}"
    return CalDavConnector({"url": url, "username": user, "password_env": PASSWORD_ENV, **extra})


def items(n: int = 6) -> list[Any]:
    cal = calendar_item("s", "c", {"name": "Work"})
    return [cal] + [
        event_item(
            "s",
            str(i),
            {"uid": f"e{i}@x", "title": f"Event {i}", "start": "2026-03-02"},
            parent=cal.usmp_id,
        )
        for i in range(n)
    ]


def interfere(monkeypatch: pytest.MonkeyPatch, trouble: Any) -> None:
    """Route every request through ``trouble(send, url, method, ...)``."""

    def request(self: Any, url: str, method: str = "GET", *args: Any, **kwargs: Any) -> Any:
        return trouble(lambda: ORIGINAL(self, url, method, *args, **kwargs), url, method)

    monkeypatch.setattr(DAVClient, "request", request)


def test_lost_answers_to_new_events_are_recognised(
    radicale: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url, _ = radicale
    lost = {"count": 0}

    def drop_two_answers(send: Any, _url: str, method: str) -> Any:
        response = send()
        if method == "PUT" and lost["count"] < 2:
            lost["count"] += 1
            raise ConnectionResetError("connection reset after the server stored it")
        return response

    interfere(monkeypatch, drop_two_answers)
    source = MemoryConnector({"domain": "calendar"}, items=items())

    result = migrate(source, connector(url), bundle_path=tmp_path / "b.usmp")

    assert lost["count"] == 2
    assert result.report is not None and result.report.ok, result.report
    assert result.report.summary.verified == 7


def test_rate_limits_and_busy_servers_are_retried(
    radicale: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, _setup: list[float]
) -> None:
    url, _ = radicale
    trouble = ["429", "503", "502"]

    def busy(send: Any, request_url: str, method: str) -> Any:
        if method == "PROPFIND" and trouble:
            kind = trouble.pop(0)
            if kind == "502":
                return _Answer(502, {"Retry-After": "4"})
            raise caldav_error.RateLimitError(
                url=request_url, retry_after_seconds=9 if kind == "429" else None, status=int(kind)
            )
        return send()

    interfere(monkeypatch, busy)

    assert list(connector(url).open_reader().read("calendar")) == []
    assert trouble == []
    assert _setup[0] == 9 and _setup[2] >= 4 and len(_setup) == 3


def test_creating_a_calendar_is_not_repeated_after_a_dropped_connection(
    radicale: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _ = radicale

    def drop(send: Any, _url: str, method: str) -> Any:
        if method == "MKCALENDAR":
            raise ConnectionResetError("reset")
        return send()

    interfere(monkeypatch, drop)

    with pytest.raises(ConnectorError, match="CalDAV error while creating calendar 'Work'"):
        list(connector(url).open_writer().write(items(0)))


def test_gives_up_when_the_server_stays_down(
    radicale: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _ = radicale
    interfere(monkeypatch, lambda send, u, m: _Answer(503, {}))

    with pytest.raises(ConnectorError, match=r"PROPFIND request kept failing.*HTTP 503"):
        list(connector(url).open_reader().read("calendar"))


def test_a_conflict_with_someone_elses_event_is_not_taken_as_ours(
    radicale: tuple[str, Path],
) -> None:
    url, _ = radicale
    writer: Any = connector(url).open_writer()
    cal, _ = writer._make({}, "Fresh")

    assert writer._holds(str(cal.url) + "missing.ics", ["some-id"]) is False


class _Answer:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers


class _Cal:
    url = "https://dav.example/cal/"


def _writer(workers: int) -> Any:
    return _CalDavWriter(client=None, only=None, workers=workers)  # type: ignore[arg-type]


def _jobs(n: int) -> list[tuple[Any, str, list[Any]]]:
    return [(_Cal(), f"e{i}@x", [ev]) for i, ev in enumerate(items(n)[1:])]


def test_new_calendars_are_filled_in_parallel() -> None:
    writer = _writer(4)
    active, peak, lock = [0], [0], threading.Lock()

    def store(cal: Any, uid: str, group: list[Any]) -> list[WriteResult]:
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return [WriteResult(group[0].usmp_id, uid)]

    writer._store = store
    results = list(writer._store_parallel(_jobs(12)))

    assert sorted(r.native_id for r in results) == sorted(f"e{i}@x" for i in range(12))
    assert 1 < peak[0] <= 4


def test_a_failed_upload_stops_new_ones_and_reports_finished_ones() -> None:
    writer = _writer(2)
    started: list[str] = []

    def store(cal: Any, uid: str, group: list[Any]) -> list[WriteResult]:
        started.append(uid)
        if uid == "e1@x":
            raise ConnectorError("the server refused event e1@x (HTTP 507)")
        time.sleep(0.01)
        return [WriteResult(group[0].usmp_id, uid)]

    writer._store = store
    yielded: list[str] = []
    with pytest.raises(ConnectorError, match="HTTP 507"):
        for result in writer._store_parallel(_jobs(40)):
            yielded.append(result.native_id)

    assert len(started) < 40, "no new uploads after the failure"
    assert "e1@x" not in yielded
    assert set(yielded) == set(started) - {"e1@x"}, "every stored event is reported"


def test_one_worker_uploads_in_order() -> None:
    writer = _writer(1)
    writer._store = lambda cal, uid, group: [WriteResult(group[0].usmp_id, uid)]

    assert [r.native_id for r in writer._store_parallel(_jobs(3))] == ["e0@x", "e1@x", "e2@x"]


@pytest.mark.parametrize("value", ["0", "17", "four", "-1"])
def test_workers_must_be_a_sensible_number(value: str) -> None:
    with pytest.raises(ConnectorError, match="workers must be a whole number from 1 to 16"):
        connector("https://dav.example", workers=value).open_writer()


def test_workers_default_and_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caldav, "_RetryingClient", lambda **kwargs: object())
    assert connector("https://dav.example").open_writer()._workers == 4  # type: ignore[attr-defined]
    writer = connector("https://dav.example", workers="8").open_writer()
    assert writer._workers == 8  # type: ignore[attr-defined]
