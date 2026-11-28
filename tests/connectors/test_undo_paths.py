"""Undo (Phase 3.4) on every calendar target.

A synthetic calendar is migrated, then undone: everything the migration created goes, and nothing
else does. Events changed on the target after the migration, events the user added, and recurring
events with a changed occurrence all stay.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import icalendar
import pytest
from caldav.davclient import DAVClient

from tests.connectors.fake_google import NO_WAIT, FakeGoogle
from usmp.connectors import gcal
from usmp.connectors._ical import NOT_EMPTY, SHARED_SERIES
from usmp.connectors.caldav import CalDavConnector
from usmp.connectors.gcal import GoogleCalendarConnector
from usmp.connectors.ics import IcsConnector
from usmp.connectors.memory import MemoryConnector
from usmp.core.engine import migrate
from usmp.core.models import UndoOutcome, UndoReport
from usmp.core.progress import read_progress_items
from usmp.core.undo import CHANGED, undo
from usmp.sdk.connector import Connector
from usmp.testing import synthetic

EVENTS = 40
SEED = 1  # a calendar with a moved occurrence
PASSWORD_ENV = "USMP_TEST_CALDAV_PASSWORD"  # noqa: S105 - a variable name
USER_EVENT = "\r\n".join(
    [
        "BEGIN:VEVENT",
        "UID:mine@example.com",
        "DTSTAMP:20260101T000000Z",
        "DTSTART:20260301T090000Z",
        "SUMMARY:Added by me",
        "END:VEVENT",
    ]
)


def source() -> MemoryConnector:
    return MemoryConnector(
        {"domain": "calendar"}, items=synthetic.calendar_items(EVENTS, seed=SEED)
    )


def written(bundle: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    """``{usmp_id: (native id on the target, migrated payload)}`` of every migrated item."""
    record = read_progress_items(bundle)
    assert record is not None
    payloads = {i.usmp_id: dict(i.payload) for i in synthetic.calendar_items(EVENTS, seed=SEED)}
    return {k: (w.native_id, payloads.get(k, {})) for k, w in record[1].items()}


def plain_event(bundle: Path) -> tuple[str, str]:
    """An event that is not part of a recurring series: ``(usmp_id, native id)``."""
    return next(
        (k, native)
        for k, (native, p) in sorted(written(bundle).items())
        if "uid" in p and "recurrence_rules" not in p and "recurrence_id" not in p
    )


def moved_occurrence(bundle: Path) -> tuple[str, str, str]:
    """A moved occurrence: ``(usmp_id, native id, usmp_id of its series)``."""
    items = written(bundle)
    occurrence, (native, payload) = next(
        (k, v) for k, v in sorted(items.items()) if "recurrence_id" in v[1]
    )
    series = next(
        k for k, (_, p) in items.items()
        if p.get("uid") == payload["uid"] and "recurrence_id" not in p
    )  # fmt: skip
    return occurrence, native, series


def calendar_native(bundle: Path) -> str:
    return next(native for native, p in written(bundle).values() if "name" in p)


def outcomes(report: UndoReport) -> dict[str, tuple[UndoOutcome, str | None]]:
    return {r.usmp_id: (r.outcome, r.reason) for r in report.items}


def assert_everything_removed(target: Connector, bundle: Path) -> UndoReport:
    result = undo(target, bundle_path=bundle)
    report = result.report
    assert report is not None and report.ok, report
    assert [r for r in report.items if r.outcome is UndoOutcome.KEPT] == []
    assert report.summary.removed == EVENTS + 1
    return report


def assert_only_ours_removed(target: Connector, bundle: Path, edited: str) -> UndoReport:
    result = undo(target, bundle_path=bundle)
    report = result.report
    assert report is not None and report.ok, report
    found = outcomes(report)
    assert found[edited] == (UndoOutcome.KEPT, CHANGED)
    calendar = next(r.usmp_id for r in report.items if r.type == "calendar")
    assert found[calendar] == (UndoOutcome.KEPT, NOT_EMPTY)
    assert report.summary.removed == EVENTS - 1
    return report


# --- iCalendar files ---------------------------------------------------------------------------


def edit_ics(path: Path, native_id: str, summary: str | None = None, add: bool = False) -> None:
    file = path / native_id.split("#", 1)[0] if path.is_dir() else path
    cal = icalendar.Calendar.from_ical(file.read_bytes())
    uid, *rid = native_id.split("#")[1:]
    for ev in cal.walk("VEVENT"):
        same = str(ev.get("UID")) == uid and (ev.get("RECURRENCE-ID") is not None) == bool(rid)
        if summary is not None and same:
            ev["SUMMARY"] = summary
    if add:
        cal.add_component(icalendar.Event.from_ical(USER_EVENT))
    file.write_bytes(cal.to_ical())


def test_ics_folder_is_cleaned_up_completely(tmp_path: Path) -> None:
    target = IcsConnector({"path": str(tmp_path / "out")})
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")

    assert_everything_removed(target, tmp_path / "a.usmp")

    assert list((tmp_path / "out").iterdir()) == []


def test_ics_keeps_changed_and_added_events(tmp_path: Path) -> None:
    out = tmp_path / "out"
    target = IcsConnector({"path": str(out)})
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")
    edited, native = plain_event(tmp_path / "a.usmp")
    edit_ics(out, native, summary="Changed by me", add=True)

    assert_only_ours_removed(target, tmp_path / "a.usmp", edited)

    [left] = out.iterdir()
    events = icalendar.Calendar.from_ical(left.read_bytes()).walk("VEVENT")
    assert sorted(str(ev["SUMMARY"]) for ev in events) == ["Added by me", "Changed by me"]


def test_ics_single_file_keeps_a_series_with_a_changed_occurrence(tmp_path: Path) -> None:
    out = tmp_path / "cal.ics"
    target = IcsConnector({"path": str(out)})
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")
    occurrence, native, series = moved_occurrence(tmp_path / "a.usmp")
    edit_ics(out, native, summary="Moved again")

    report = undo(target, bundle_path=tmp_path / "a.usmp").report

    assert report is not None
    found = outcomes(report)
    assert found[occurrence] == (UndoOutcome.KEPT, CHANGED)
    assert found[series] == (UndoOutcome.KEPT, SHARED_SERIES)
    assert out.is_file()
    assert len(icalendar.Calendar.from_ical(out.read_bytes()).walk("VEVENT")) == 2


def test_undoing_a_rerun_removes_nothing(tmp_path: Path) -> None:
    target = IcsConnector({"path": str(tmp_path / "out")})
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")
    again = migrate(source(), target, bundle_path=tmp_path / "b.usmp")
    assert again.report is not None and again.report.summary.already_present == EVENTS + 1

    report = undo(target, bundle_path=tmp_path / "b.usmp").report

    assert report is not None
    assert report.summary.total == 0 and report.summary.left_alone == EVENTS + 1
    assert len(list((tmp_path / "out").iterdir())) == 1


# --- CalDAV ------------------------------------------------------------------------------------


@pytest.fixture
def dav(
    radicale: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Callable[[], CalDavConnector], DAVClient]:
    monkeypatch.setenv(PASSWORD_ENV, "test")
    url, _ = radicale
    user = f"undo-{os.urandom(4).hex()}"
    client = DAVClient(url=url, username=user, password="test")  # noqa: S106 - test server
    config = {"url": url, "username": user, "password_env": PASSWORD_ENV}
    return (lambda: CalDavConnector(config)), client


def test_caldav_is_cleaned_up_completely(
    tmp_path: Path, dav: tuple[Callable[[], CalDavConnector], DAVClient]
) -> None:
    make, client = dav
    migrate(source(), make(), bundle_path=tmp_path / "a.usmp")

    assert_everything_removed(make(), tmp_path / "a.usmp")

    assert client.principal().calendars() == []  # type: ignore[no-untyped-call]


def test_caldav_keeps_changed_and_added_events(
    tmp_path: Path, dav: tuple[Callable[[], CalDavConnector], DAVClient]
) -> None:
    make, client = dav
    migrate(source(), make(), bundle_path=tmp_path / "a.usmp")
    edited, native = plain_event(tmp_path / "a.usmp")
    url = native.split("#", 1)[0]
    text = client.request(url, "GET").raw
    changed = text.replace("\nSUMMARY:", "\nSUMMARY:Changed by me ", 1)
    client.put(url, changed, {"Content-Type": "text/calendar"})
    cal = client.calendar(url=calendar_native(tmp_path / "a.usmp"))  # type: ignore[no-untyped-call]
    cal.save_event("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n" + USER_EVENT
                   + "\r\nEND:VCALENDAR\r\n")  # fmt: skip

    assert_only_ours_removed(make(), tmp_path / "a.usmp", edited)

    assert len(cal.events()) == 2


# --- Google ------------------------------------------------------------------------------------


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    fake = FakeGoogle()
    monkeypatch.setattr(
        GoogleCalendarConnector, "session_for", staticmethod(lambda account, write: fake.session)
    )
    monkeypatch.setattr(gcal._Api, "policy", NO_WAIT)
    with fake:
        yield fake


def google_target() -> GoogleCalendarConnector:
    return GoogleCalendarConnector({"account": "me@example.com"})


def test_google_is_cleaned_up_completely(tmp_path: Path, google: FakeGoogle) -> None:
    migrate(source(), google_target(), bundle_path=tmp_path / "a.usmp")

    assert_everything_removed(google_target(), tmp_path / "a.usmp")

    assert google.calendars == {}


def test_google_keeps_changed_and_added_events(tmp_path: Path, google: FakeGoogle) -> None:
    migrate(source(), google_target(), bundle_path=tmp_path / "a.usmp")
    edited, native = plain_event(tmp_path / "a.usmp")
    cal_id, event_id = native.split("/", 1)
    google.edit_event(cal_id, event_id, summary="Changed by me")
    google.add_event(cal_id, {"id": "mine", "iCalUID": "mine@example.com", "summary": "Mine",
                              "status": "confirmed", "start": {"date": "2026-03-01"},
                              "end": {"date": "2026-03-02"}})  # fmt: skip

    assert_only_ours_removed(google_target(), tmp_path / "a.usmp", edited)

    assert set(google.events[cal_id]) == {event_id, "mine"}


def test_google_keeps_a_series_with_a_changed_occurrence(
    tmp_path: Path, google: FakeGoogle
) -> None:
    migrate(source(), google_target(), bundle_path=tmp_path / "a.usmp")
    occurrence, native, series = moved_occurrence(tmp_path / "a.usmp")
    cal_id, event_id = native.split("/", 1)
    google.edit_event(cal_id, event_id, summary="Moved again")

    report = undo(google_target(), bundle_path=tmp_path / "a.usmp").report

    assert report is not None
    found = outcomes(report)
    assert found[occurrence] == (UndoOutcome.KEPT, CHANGED)
    assert found[series] == (UndoOutcome.KEPT, SHARED_SERIES)
    assert event_id in google.events[cal_id]


def test_google_never_removes_a_calendar_it_merged_into(tmp_path: Path, google: FakeGoogle) -> None:
    google.add_calendar("mine@group.calendar.google.com", "Mine")
    target = GoogleCalendarConnector({"account": "me@example.com", "calendar": "Mine"})
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")

    report = undo(target, bundle_path=tmp_path / "a.usmp").report

    assert report is not None and report.summary.removed == EVENTS
    assert "mine@group.calendar.google.com" in google.calendars
    assert google.events["mine@group.calendar.google.com"] == {}


def test_caldav_without_the_calendar_marker_relies_on_the_record(
    tmp_path: Path,
    dav: tuple[Callable[[], CalDavConnector], DAVClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caldav import Calendar
    from caldav.lib import error as caldav_error

    make, _ = dav

    def refuse(self: Any, props: Any = None) -> None:
        raise caldav_error.PropsetError("custom properties are not allowed")

    with monkeypatch.context() as patch:
        patch.setattr(Calendar, "set_properties", refuse)
        migrate(source(), make(), bundle_path=tmp_path / "a.usmp")

    assert_everything_removed(make(), tmp_path / "a.usmp")


def test_caldav_never_removes_a_calendar_it_merged_into(
    tmp_path: Path, dav: tuple[Callable[[], CalDavConnector], DAVClient]
) -> None:
    make, client = dav
    client.principal().make_calendar(name="Mine")  # type: ignore[no-untyped-call]
    merged = CalDavConnector({**make().config, "calendar": "Mine"})
    migrate(source(), merged, bundle_path=tmp_path / "a.usmp")

    report = undo(merged, bundle_path=tmp_path / "a.usmp").report

    assert report is not None and report.summary.removed == EVENTS
    [cal] = client.principal().calendars()  # type: ignore[no-untyped-call]
    assert cal.events() == []
