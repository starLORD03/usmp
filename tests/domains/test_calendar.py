from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.conftest import make_capabilities
from usmp import domains
from usmp.connectors.memory import MemoryConnector
from usmp.core.engine import MigrationError, migrate
from usmp.core.models import Item
from usmp.domains.calendar import calendar_item, canonicalize, event_item, problems
from usmp.sdk.connector import ExportRecord, Reader
from usmp.testing import ConformanceError, check_read

BASE: dict[str, Any] = {"uid": "evt-1@example.com", "start": "2026-01-05T09:00:00"}


def event(**fields: Any) -> dict[str, Any]:
    return canonicalize("event", {**BASE, **fields})


class TestCanonicalForm:
    def test_minimal_event_is_unchanged(self) -> None:
        assert event() == BASE

    def test_defaults_blanks_and_nulls_are_dropped(self) -> None:
        assert (
            event(description="  ", location=None, attendees=[], categories=[], status=None) == BASE
        )

    def test_rrule_parts_are_ordered(self) -> None:
        assert event(recurrence_rules=["rrule:byday=MO,WE;interval=2;freq=weekly"])[
            "recurrence_rules"
        ] == ["FREQ=WEEKLY;BYDAY=MO,WE;INTERVAL=2"]

    def test_lists_are_sorted_and_deduplicated(self) -> None:
        e = event(
            categories=["work", "Admin", "work"],
            excluded_dates=["2026-01-19T09:00:00", "2026-01-12T09:00:00"],
            alarms=[{"trigger": "-pt5m"}, {"trigger": "-PT1H"}, {"trigger": "-PT5M"}],
            attendees=[
                {"email": "Zoe@Example.com", "status": "accepted"},
                {"email": "mailto:amy@example.com", "role": "optional"},
            ],
            attachments=[{"uri": "https://b"}, {"uri": "https://a", "title": ""}],
        )

        assert e["categories"] == ["Admin", "work"]
        assert e["excluded_dates"] == ["2026-01-12T09:00:00", "2026-01-19T09:00:00"]
        assert e["alarms"] == [{"trigger": "-PT1H"}, {"trigger": "-PT5M"}]
        assert e["attendees"] == [
            {"email": "amy@example.com", "role": "optional"},
            {"email": "zoe@example.com", "status": "accepted"},
        ]
        assert e["attachments"] == [{"uri": "https://a"}, {"uri": "https://b"}]

    def test_utc_aliases_and_colors(self) -> None:
        e = event(time_zone="Etc/UTC", color="#1A73E8")

        assert e["time_zone"] == "UTC"
        assert e["color"] == "#1a73e8"

    def test_same_event_from_two_providers_hashes_the_same(self) -> None:
        google = event_item(
            "google",
            "g1",
            {
                **BASE,
                "title": "Standup",
                "time_zone": "Etc/UTC",
                "recurrence_rules": ["RRULE:INTERVAL=1;FREQ=DAILY"],
                "attendees": [{"email": "A@x.com"}],
                "description": "",
            },
        )
        apple = event_item(
            "caldav",
            "a1",
            {
                **BASE,
                "title": "Standup",
                "time_zone": "UTC",
                "recurrence_rules": ["FREQ=DAILY;INTERVAL=1"],
                "attendees": [{"email": "a@x.com", "role": "required"}],
            },
        )

        assert google.content_hash == apple.content_hash

    def test_all_day_event(self) -> None:
        e = event(start="2026-02-01", end="2026-02-02")

        assert e["start"] == "2026-02-01"

    def test_calendar(self) -> None:
        cal = calendar_item("x", "c", {"name": "Work", "color": "#FFFFFF", "description": ""})

        assert cal.payload == {"name": "Work", "color": "#ffffff"}
        assert cal.type == "calendar"


class TestValidation:
    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"start": "2026-01-05T09:00:00Z"}, "without an offset"),
            ({"start": "2026-13-05"}, "month must be in"),
            ({"end": "2026-01-05T08:00:00"}, "end must be after start"),
            ({"start": "2026-01-05", "end": "2026-01-06T00:00:00"}, "end must be a date"),
            (
                {"start": "2026-01-05", "time_zone": "Europe/Paris"},
                "all-day events have no time zone",
            ),
            ({"time_zone": "Mars/Olympus"}, "unknown IANA time zone"),
            ({"recurrence_rules": ["INTERVAL=2"]}, "needs a valid FREQ"),
            ({"recurrence_rules": ["FREQ=DAILY;FREQ=WEEKLY"]}, "repeats"),
            ({"recurrence_rules": ["FREQ=DAILY;COUNT=2;UNTIL=20260101"]}, "both UNTIL and COUNT"),
            ({"recurrence_rules": ["FREQ=DAILY;COLOR=red"]}, "unknown recurrence rule part"),
            ({"recurrence_rules": ["FREQ=DAILY;COUNT"]}, "invalid recurrence rule part"),
            (
                {"recurrence_id": "2026-01-05", "start": "2026-01-06T09:00:00"},
                "recurrence_id must be",
            ),
            (
                {"recurrence_id": "2026-01-05T09:00:00", "recurrence_rules": ["FREQ=DAILY"]},
                "cannot have recurrence rules",
            ),
            ({"excluded_dates": ["2026-01-12"]}, "recurrence and excluded dates"),
            ({"attendees": [{"email": "a@x.com"}, {"email": "A@x.com"}]}, "unique email"),
            ({"attendees": [{"email": "not-an-email"}]}, "not an email"),
            ({"alarms": [{"trigger": "15 minutes"}]}, "ISO 8601 duration"),
            ({"color": "blue"}, "must be a colour"),
            ({"end_time_zone": "Europe/Paris"}, "end_time_zone needs end"),
            (
                {"time_zone": "UTC", "end": "2026-01-05T10:00:00", "end_time_zone": "UTC"},
                "omit end_time_zone",
            ),
            ({"surprise": 1}, "Extra inputs"),
        ],
    )
    def test_invalid_events(self, fields: dict[str, Any], message: str) -> None:
        found = problems("event", {**BASE, **fields})

        assert found, fields
        assert any(message in p for p in found), found

    def test_different_end_zone_skips_order_check(self) -> None:
        e = event(
            time_zone="America/New_York", end="2026-01-05T08:00:00", end_time_zone="Europe/London"
        )

        assert e["end_time_zone"] == "Europe/London"

    def test_non_canonical_payload_is_reported(self) -> None:
        [problem] = problems("event", {**BASE, "categories": ["b", "a"]})

        assert "not in canonical form (fields: categories)" in problem

    def test_unknown_type(self) -> None:
        assert problems("todo", {}) == [
            "unknown calendar item type 'todo'; expected one of ['calendar', 'event']"
        ]

    def test_valid_payload_has_no_problems(self) -> None:
        assert problems("event", event(title="ok")) == []


zones = st.sampled_from(["UTC", "Europe/Paris", "America/New_York", "Asia/Kolkata"])
words = st.text(alphabet="abcdefghij KLM", min_size=1, max_size=12)


@st.composite
def raw_events(draw: st.DrawFn) -> dict[str, Any]:
    day = draw(st.integers(1, 27))
    hour = draw(st.integers(0, 21))
    payload: dict[str, Any] = {
        "uid": draw(words) + "@test",
        "start": f"2026-03-{day:02d}T{hour:02d}:00:00",
        "end": f"2026-03-{day:02d}T{hour + 1:02d}:30:00",
        "title": draw(words),
        "time_zone": draw(zones),
        "categories": draw(st.lists(words, max_size=4)),
        "attendees": [
            {"email": f"{name}@Example.com"}
            for name in draw(st.lists(st.sampled_from("abcdef"), unique=True, max_size=4))
        ],
        "recurrence_rules": draw(
            st.lists(st.sampled_from(["interval=2;freq=weekly", "FREQ=DAILY;COUNT=5"]), max_size=1)
        ),
    }
    return payload


@given(raw_events())
def test_canonicalize_is_idempotent(raw: dict[str, Any]) -> None:
    once = canonicalize("event", raw)

    assert canonicalize("event", once) == once
    assert problems("event", once) == []


@given(raw_events(), st.randoms())
def test_order_of_lists_does_not_change_hash(raw: dict[str, Any], rnd: Any) -> None:
    shuffled = {
        **raw,
        "categories": rnd.sample(raw["categories"], len(raw["categories"])),
        "attendees": rnd.sample(raw["attendees"], len(raw["attendees"])),
    }

    assert event_item("a", "1", raw).content_hash == event_item("b", "2", shuffled).content_hash


class TestDomainRegistry:
    def test_known_and_unknown_domains(self) -> None:
        bad = Item.create(
            connector="m",
            native_id="1",
            domain="calendar",
            type="event",
            schema_version=1,
            payload={"title": "no uid"},
        )
        other = Item.create(
            connector="m",
            native_id="1",
            domain="notes",
            type="note",
            schema_version=1,
            payload={"anything": True},
        )

        assert domains.validate(bad)
        assert domains.validate(other) == []
        assert domains.get("calendar", 2) is None

    def calendar_source(self, payload: dict[str, Any]) -> MemoryConnector:
        item = Item.create(
            connector="memory",
            native_id="1",
            domain="calendar",
            type="event",
            schema_version=1,
            payload=payload,
        )

        class Source(MemoryConnector):
            def open_reader(self) -> Reader:
                class R(Reader):
                    def read(self, domain: str) -> Iterator[ExportRecord]:
                        yield ExportRecord(item=item)

                return R()

        caps = make_capabilities("memory", read=["*"], write=["*"])
        caps = caps.model_copy(
            update={"domains": [caps.domains[0].model_copy(update={"domain": "calendar"})]}
        )
        return Source(capabilities=caps)

    def test_engine_rejects_invalid_calendar_items(self, tmp_path: Path) -> None:
        source = self.calendar_source({"uid": "x", "start": "yesterday"})

        with pytest.raises(MigrationError, match="invalid calendar 'event' item"):
            migrate(
                source, MemoryConnector({"domain": "calendar"}), bundle_path=tmp_path / "b.usmp"
            )

    def test_kit_rejects_invalid_calendar_items(self) -> None:
        source = self.calendar_source({"uid": "x", "start": "2026-01-01", "categories": ["b", "a"]})

        with pytest.raises(ConformanceError, match="invalid 'event' item"):
            check_read(source, "calendar")


def test_kit_rejects_invalid_read_back() -> None:
    from collections.abc import Iterable, Sequence

    from usmp.sdk.connector import Writer, WriteResult
    from usmp.testing import check_round_trip

    good = event_item("memory", "1", {**BASE, "title": "ok"})
    bad = Item.create(
        connector="memory",
        native_id="n0",
        domain="calendar",
        type="event",
        schema_version=1,
        payload={"uid": "x", "start": "nope"},
    )

    class BadWriter(Writer):
        def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
            yield from (WriteResult(usmp_id=i.usmp_id, native_id="n0") for i in items)

        def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
            yield bad

    class Target(MemoryConnector):
        def open_writer(self) -> Writer:
            return BadWriter()

    with pytest.raises(ConformanceError, match="read_back\\(\\) returned an invalid item"):
        check_round_trip(Target({"domain": "calendar"}), [good], "calendar")
