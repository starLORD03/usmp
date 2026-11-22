"""Release test (Phase 2 exit criterion): a 1,000-event calendar along every supported path.

Each path must end with 0 mismatched and 0 missing items. Google runs against the simulated API
and CalDAV against a real Radicale server. Real iCloud and Nextcloud accounts are checked by hand
(see the guides).
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.connectors.fake_google import FakeGoogle
from usmp.connectors import gcal
from usmp.connectors.caldav import CalDavConnector
from usmp.connectors.gcal import GoogleCalendarConnector
from usmp.connectors.ics import IcsConnector
from usmp.connectors.memory import MemoryConnector
from usmp.core.engine import MigrationResult, migrate
from usmp.sdk.connector import Connector
from usmp.testing import synthetic

# CI runs the full 1,000 in its own job; everyday runs use a smaller calendar.
EVENTS = int(os.environ.get("USMP_RELEASE_EVENTS", "200"))
PASSWORD_ENV = "USMP_TEST_CALDAV_PASSWORD"  # noqa: S105 - a variable name


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    fake = FakeGoogle()
    monkeypatch.setattr(
        GoogleCalendarConnector, "session_for", staticmethod(lambda account, write: fake.session)
    )
    monkeypatch.setattr(gcal.time, "sleep", lambda seconds: None)
    with fake:
        yield fake


@pytest.fixture
def caldav(radicale: tuple[str, Path], monkeypatch: pytest.MonkeyPatch) -> CalDavConnector:
    monkeypatch.setenv(PASSWORD_ENV, "test")
    url, _ = radicale
    user = f"release-{os.urandom(4).hex()}"
    return CalDavConnector({"url": url, "username": user, "password_env": PASSWORD_ENV})


def run(source: Connector, target: Connector, tmp_path: Path, name: str) -> MigrationResult:
    result = migrate(source, target, bundle_path=tmp_path / f"{name}.usmp")
    report = result.report
    assert report is not None
    assert report.summary.total == EVENTS + 1, report.summary
    assert report.summary.mismatch == 0, [i for i in report.items if i.status == "mismatch"][:3]
    assert report.summary.missing == 0, [i for i in report.items if i.status == "missing"][:3]
    assert report.ok, report.errors
    return result


def seed_google(tmp_path: Path) -> GoogleCalendarConnector:
    """Put the synthetic calendar into the simulated Google account."""
    google = GoogleCalendarConnector({"account": "me@example.com"})
    source = MemoryConnector({"domain": "calendar"}, items=synthetic.calendar_items(EVENTS))
    run(source, google, tmp_path, "seed")
    return google


def test_synthetic_calendar_is_varied_and_stable() -> None:
    items = synthetic.calendar_items(EVENTS)
    events: list[dict[str, Any]] = [dict(i.payload) for i in items if i.type == "event"]

    assert len(events) == EVENTS
    assert synthetic.calendar_items(EVENTS) == items, "same seed, same calendar"
    assert any("recurrence_id" in e for e in events), "moved occurrences"
    assert any(e.get("excluded_dates") for e in events), "cancelled occurrences"
    assert any("end_time_zone" in e for e in events), "events ending in another zone"
    assert any("T" not in e["start"] for e in events), "all-day events"
    assert any(e.get("conferences") for e in events), "meeting links"
    assert any(a.get("kind") == "room" for e in events for a in e.get("attendees", [])), "rooms"


def test_ics_file_to_caldav(tmp_path: Path, caldav: CalDavConnector) -> None:
    path = tmp_path / "test-calendar.ics"
    synthetic.main([str(path), "--events", str(EVENTS)])

    result = run(IcsConnector({"path": str(path)}), caldav, tmp_path, "ics-caldav")

    assert result.report is not None
    assert result.report.summary.verified_with_declared_loss == 0, "CalDAV keeps everything"


def test_google_to_caldav(tmp_path: Path, google: FakeGoogle, caldav: CalDavConnector) -> None:
    run(seed_google(tmp_path), caldav, tmp_path, "google-caldav")


def test_google_to_ics_file(tmp_path: Path, google: FakeGoogle) -> None:
    target = IcsConnector({"path": str(tmp_path / "out")})

    result = run(seed_google(tmp_path), target, tmp_path, "google-ics")

    assert result.report is not None
    assert result.report.summary.verified_with_declared_loss == 0, "iCalendar keeps everything"


def test_generator_will_not_overwrite_a_file(tmp_path: Path) -> None:
    path = tmp_path / "taken.ics"
    path.write_text("keep me")

    with pytest.raises(SystemExit):
        synthetic.main([str(path), "--events", "5"])
    assert path.read_text() == "keep me"
