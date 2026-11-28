from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from usmp.core.models import Item, Selection
from usmp.core.selection import SelectionError, make_selection
from usmp.domains import select
from usmp.domains.calendar import calendar_item, event_item

PLUS1 = timezone(timedelta(hours=1))
WORK = calendar_item("s", "work", {"name": "Work"})
HOME = calendar_item("s", "home", {"name": "Home"})


def event(native_id: str, cal: Item = WORK, /, **payload: Any) -> Item:
    return event_item("s", native_id, {"uid": f"{native_id}@x", **payload}, parent=cal.usmp_id)


def between(since: str | None, until: str | None, *names: str) -> Selection:
    return Selection(
        since=datetime.fromisoformat(since).replace(tzinfo=PLUS1) if since else None,
        until=datetime.fromisoformat(until).replace(tzinfo=PLUS1) if until else None,
        collections=list(names),
    )


def kept(items: list[Item], selection: Selection) -> set[str]:
    by_id = {i.usmp_id: i.source.native_id for i in items}
    return {by_id[k] for k in select("calendar", items, selection)}


YEAR_2026 = between("2026-01-01T00:00", "2027-01-01T00:00")


@pytest.mark.parametrize(
    ("payload", "inside"),
    [
        ({"start": "2026-03-01T09:00:00", "time_zone": "Europe/Paris"}, True),
        ({"start": "2025-12-31T23:30:00", "end": "2026-01-01T00:30:00"}, True),  # floating
        ({"start": "2025-12-31T22:00:00", "end": "2025-12-31T23:30:00", "time_zone": "UTC"}, True),
        ({"start": "2025-12-31T21:00:00", "end": "2025-12-31T22:30:00", "time_zone": "UTC"}, False),
        ({"start": "2025-12-31"}, False),  # all day, ends at midnight
        ({"start": "2026-12-31"}, True),
        ({"start": "2027-01-01"}, False),
        ({"start": "2026-12-31T23:00:00", "time_zone": "UTC"}, False),  # 2027-01-01 00:00 +01
        ({"start": "2026-01-01T00:00:00"}, True),  # zero length, exactly at the start
    ],
)
def test_single_events_by_date(payload: dict[str, Any], inside: bool) -> None:
    items = [WORK, event("e", **payload)]
    assert ("e" in kept(items, YEAR_2026)) is inside


@pytest.mark.parametrize(
    ("payload", "inside"),
    [
        ({"recurrence_rules": ["FREQ=WEEKLY"]}, True),  # never ends
        ({"recurrence_rules": ["FREQ=WEEKLY;COUNT=3"]}, False),  # over in January 2025
        ({"recurrence_rules": ["FREQ=YEARLY;COUNT=2"]}, True),  # second one in 2026
        ({"recurrence_rules": ["FREQ=DAILY;UNTIL=20251231T080000Z"]}, False),
        ({"recurrence_rules": ["FREQ=DAILY;UNTIL=20260101T090000Z"]}, True),
        ({"recurrence_dates": ["2026-07-01T09:00:00"]}, True),
        ({"recurrence_dates": ["2025-07-01T09:00:00"]}, False),
    ],
)
def test_recurring_series_by_date(payload: dict[str, Any], inside: bool) -> None:
    base = {"start": "2025-01-06T09:00:00", "end": "2025-01-06T10:00:00", "time_zone": "UTC"}
    items = [WORK, event("e", **base, **payload)]
    assert ("e" in kept(items, YEAR_2026)) is inside


def test_series_is_never_cut() -> None:
    master = event("m", start="2025-01-06T09:00:00", recurrence_rules=["FREQ=WEEKLY"])
    old_override = event_item(
        "s",
        "o",
        {"uid": "m@x", "start": "2025-01-14T11:00:00", "recurrence_id": "2025-01-13T09:00:00"},
        parent=WORK.usmp_id,
    )
    other_calendar = event_item(
        "s", "h", {"uid": "m@x", "start": "2025-01-06T09:00:00"}, parent=HOME.usmp_id
    )

    result = kept([WORK, HOME, master, old_override, other_calendar], YEAR_2026)

    assert result == {"work", "m", "o"}


def test_override_inside_the_range_keeps_its_old_series() -> None:
    master = event("m", start="2025-01-06T09:00:00", recurrence_rules=["FREQ=WEEKLY;COUNT=2"])
    moved = event_item(
        "s",
        "o",
        {"uid": "m@x", "start": "2026-02-01T09:00:00", "recurrence_id": "2025-01-13T09:00:00"},
        parent=WORK.usmp_id,
    )
    assert kept([WORK, master, moved], YEAR_2026) == {"work", "m", "o"}


def test_calendars_by_name_ignore_case_and_keep_empty_named_ones() -> None:
    items = [WORK, HOME, event("w", start="2026-01-01"), event("h", HOME, start="2026-01-01")]

    assert kept(items, between(None, None, "work")) == {"work", "w"}
    assert kept(items, between("2030-01-01T00:00", None, "Home")) == {"home"}
    assert kept(items, between("2030-01-01T00:00", None)) == set()


def test_unknown_calendar_is_explained() -> None:
    with pytest.raises(ValueError, match="no calendar named 'Gym'; available: Home, Work"):
        select("calendar", [WORK, HOME], between(None, None, "Gym"))


def test_events_without_a_calendar() -> None:
    orphan = event_item("s", "o", {"uid": "o@x", "start": "2026-05-01"})
    assert kept([WORK, orphan], YEAR_2026) == {"o"}
    assert kept([WORK, orphan], between(None, None, "Work")) == {"work"}


def test_times_that_cannot_be_worked_out_are_kept() -> None:
    odd = event("e", start="2025-01-06T09:00:00", recurrence_rules=["FREQ=WEEKLY;BYDAY=XX"])
    assert "e" in kept([WORK, odd], YEAR_2026)


def test_unknown_domains_cannot_select() -> None:
    with pytest.raises(ValueError, match="cannot choose part of 'demo' data"):
        select("demo", [], YEAR_2026)


class TestMakeSelection:
    def test_dates_include_the_whole_until_day(self) -> None:
        selection = make_selection("2026-01-01", "2026-12-31", [" Work ", ""])
        assert selection is not None
        assert selection.since is not None and selection.until is not None
        assert selection.since.replace(tzinfo=None) == datetime(2026, 1, 1)
        assert selection.until.replace(tzinfo=None) == datetime(2027, 1, 1)
        assert selection.collections == ["Work"]
        assert (
            selection.describe()
            == "from 2026-01-01; until 2027-01-01 (not included); calendars: Work"
        )

    def test_times_with_and_without_offsets(self) -> None:
        selection = make_selection("2026-01-01 08:30+02:00", "2026-01-02T10:00")
        assert selection is not None and selection.since is not None
        assert selection.since.utcoffset() == timedelta(hours=2)
        assert "2026-01-01T08:30+02:00" in selection.describe()

    def test_nothing_chosen_is_none(self) -> None:
        assert make_selection(None, None, []) is None
        assert Selection().describe() == "everything"

    @pytest.mark.parametrize(
        ("since", "until", "message"),
        [
            ("yesterday", None, "'yesterday' is not a date"),
            ("2026-02-01", "2026-01-01", "since must be earlier than until"),
        ],
    )
    def test_bad_answers(self, since: str, until: str | None, message: str) -> None:
        with pytest.raises(SelectionError, match=message):
            make_selection(since, until)


def test_until_only_and_an_empty_selection() -> None:
    items = [WORK, HOME, event("old", start="2020-01-01"), event("new", start="2030-01-01")]
    assert kept(items, between(None, "2025-01-01T00:00")) == {"work", "old"}
    assert kept(items, Selection()) == {"work", "home", "old", "new"}
