"""Safe re-runs and resume (Phase 3.1) on every calendar target.

A migration is stopped part-way, as if by Ctrl+C or a dropped connection, and then resumed from
its backup; or it is simply run a second time. Either way nothing may be written twice and the
final report must match an uninterrupted run.
"""

import os
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import pytest

from tests.connectors.fake_google import NO_WAIT, FakeGoogle
from usmp.connectors import gcal
from usmp.connectors.caldav import CalDavConnector
from usmp.connectors.gcal import GoogleCalendarConnector
from usmp.connectors.ics import IcsConnector
from usmp.connectors.memory import MemoryConnector
from usmp.core.engine import MigrationResult, migrate, resume
from usmp.core.models import Item
from usmp.core.progress import read_progress
from usmp.domains.calendar import calendar_item, event_item
from usmp.sdk.connector import Connector, Writer, WriteResult
from usmp.testing import synthetic

EVENTS = 40
PASSWORD_ENV = "USMP_TEST_CALDAV_PASSWORD"  # noqa: S105 - a variable name


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    fake = FakeGoogle()
    monkeypatch.setattr(
        GoogleCalendarConnector, "session_for", staticmethod(lambda account, write: fake.session)
    )
    monkeypatch.setattr(gcal._Api, "policy", NO_WAIT)
    with fake:
        yield fake


@pytest.fixture
def make_caldav(
    radicale: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> Callable[[], CalDavConnector]:
    monkeypatch.setenv(PASSWORD_ENV, "test")
    url, _ = radicale
    user = f"resume-{os.urandom(4).hex()}"
    return lambda: CalDavConnector({"url": url, "username": user, "password_env": PASSWORD_ENV})


def source() -> MemoryConnector:
    return MemoryConnector({"domain": "calendar"}, items=synthetic.calendar_items(EVENTS))


def interrupt_after(connector: Connector, limit: int) -> Connector:
    """Make ``connector``'s writer stop with KeyboardInterrupt after ``limit`` stored items."""
    original = connector.open_writer

    def open_writer() -> Writer:
        writer = original()
        write = writer.write

        def limited(items: Iterable[Item]) -> Iterator[WriteResult]:
            for n, result in enumerate(write(items)):
                if n == limit:
                    raise KeyboardInterrupt
                yield result

        writer.write = limited  # type: ignore[method-assign]
        return writer

    connector.open_writer = open_writer  # type: ignore[method-assign]
    return connector


def assert_complete(result: MigrationResult) -> None:
    report = result.report
    assert report is not None
    assert report.summary.total == EVENTS + 1
    assert report.summary.mismatch == 0, [i for i in report.items if i.status == "mismatch"][:3]
    assert report.summary.missing == 0, [i for i in report.items if i.status == "missing"][:3]
    assert report.ok, report.errors


def check_interrupt_and_resume(make: Callable[[], Connector], tmp_path: Path, limit: int) -> None:
    bundle = tmp_path / "b.usmp"
    with pytest.raises(KeyboardInterrupt):
        migrate(source(), interrupt_after(make(), limit), bundle_path=bundle)
    record = read_progress(bundle)
    assert record is not None and 0 < len(record[1]) <= limit

    resumed = resume(make(), bundle_path=bundle)

    assert_complete(resumed)
    assert resumed.report is not None and resumed.report.summary.already_present >= len(record[1])
    again = migrate(source(), make(), bundle_path=tmp_path / "again.usmp")
    assert_complete(again)
    assert again.report is not None and again.report.summary.already_present == EVENTS + 1


def test_caldav_resume_and_rerun(
    make_caldav: Callable[[], CalDavConnector], tmp_path: Path
) -> None:
    check_interrupt_and_resume(make_caldav, tmp_path, limit=15)
    target = make_caldav()
    names = [
        r.item.payload["name"]
        for r in target.open_reader().read("calendar")
        if r.item.type == "calendar"
    ]
    assert len(names) == 1, f"no duplicate calendars: {names}"


def test_google_resume_and_rerun(google: FakeGoogle, tmp_path: Path) -> None:
    check_interrupt_and_resume(
        lambda: GoogleCalendarConnector({"account": "me@example.com"}), tmp_path, limit=15
    )
    assert len(google.calendars) == 1, "no duplicate calendars"
    assert sum(len(e) for e in google.events.values()) == EVENTS


def test_ics_rerun_into_directory_and_file(tmp_path: Path) -> None:
    for path in (tmp_path / "out", tmp_path / "one.ics"):
        first = migrate(source(), IcsConnector({"path": str(path)}), bundle_path=tmp_path / "a")
        again = migrate(source(), IcsConnector({"path": str(path)}), bundle_path=tmp_path / "b")

        assert_complete(first)
        assert_complete(again)
        assert again.report is not None and again.report.summary.already_present == EVENTS + 1
    assert len(list((tmp_path / "out").iterdir())) == 1


def test_ics_interrupted_before_the_file_is_written_resumes(tmp_path: Path) -> None:
    # The file is written whole at the end, so an interrupted run leaves nothing behind.
    check_interrupt_and_resume(
        lambda: IcsConnector({"path": str(tmp_path / "out")}), tmp_path, limit=EVENTS
    )


def test_ics_refuses_to_rewrite_a_file_to_add_new_events(tmp_path: Path) -> None:
    cal = calendar_item("s", "c", {"name": "Work"})
    one = event_item("s", "1", {"uid": "1@x", "start": "2026-01-01"}, parent=cal.usmp_id)
    two = event_item("s", "2", {"uid": "2@x", "start": "2026-01-02"}, parent=cal.usmp_id)
    out = IcsConnector({"path": str(tmp_path / "out")})
    migrate(
        MemoryConnector({"domain": "calendar"}, items=[cal, one]), out, bundle_path=tmp_path / "a"
    )
    before = (tmp_path / "out" / "work.ics").read_bytes()

    later = migrate(
        MemoryConnector({"domain": "calendar"}, items=[cal, one, two]),
        out,
        bundle_path=tmp_path / "b",
    )

    assert later.report is not None and not later.report.ok
    assert any("refusing to overwrite" in e for e in later.report.errors)
    assert (tmp_path / "out" / "work.ics").read_bytes() == before


def test_caldav_finds_an_empty_calendar_it_made(
    make_caldav: Callable[[], CalDavConnector], tmp_path: Path
) -> None:
    empty = calendar_item("s", "c", {"name": "Empty"})
    first = migrate(
        MemoryConnector({"domain": "calendar"}, items=[empty]),
        make_caldav(),
        bundle_path=tmp_path / "a",
    )
    again = migrate(
        MemoryConnector({"domain": "calendar"}, items=[empty]),
        make_caldav(),
        bundle_path=tmp_path / "b",
    )

    assert first.report is not None and first.report.ok
    assert again.report is not None and again.report.ok
    assert again.report.items[0].already_present
    calendars = [r for r in make_caldav().open_reader().read("calendar")]
    assert [c.item.payload["name"] for c in calendars] == ["Empty"]


def test_caldav_rerun_keeps_declared_calendar_losses(
    make_caldav: Callable[[], CalDavConnector], tmp_path: Path
) -> None:
    taken = calendar_item("s", "t", {"name": "Work"})
    list(make_caldav().open_writer().write([taken]))
    cal = calendar_item("s", "c", {"name": "Work", "time_zone": "Europe/Paris"})
    event = event_item("s", "1", {"uid": "1@x", "start": "2026-01-01"}, parent=cal.usmp_id)
    items = [cal, event]

    first = migrate(MemoryConnector({"domain": "calendar"}, items=items), make_caldav(),
                    bundle_path=tmp_path / "a")  # fmt: skip
    again = migrate(MemoryConnector({"domain": "calendar"}, items=items), make_caldav(),
                    bundle_path=tmp_path / "b")  # fmt: skip

    assert first.report is not None and again.report is not None
    assert again.report.ok and again.report.summary.already_present == 2

    def details(result: MigrationResult) -> set[str]:
        assert result.report is not None
        return {loss.detail for loss in result.report.losses if loss.stage == "import"}

    assert details(again) == details(first)
    assert "stored as 'Work (2)'" in details(again)
