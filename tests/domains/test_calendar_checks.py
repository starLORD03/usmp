from pathlib import Path
from typing import Any

import pytest

from usmp.connectors.ics import IcsConnector
from usmp.core.engine import migrate
from usmp.core.models import Item, LossReason
from usmp.core.report import to_html, to_markdown
from usmp.domains import warnings
from usmp.domains.calendar import calendar_item, event_item

CAL = calendar_item("s", "c", {"name": "Work"})


def event(native_id: str, **payload: Any) -> Item:
    return event_item("s", native_id, {"uid": f"{native_id}@x", **payload}, parent=CAL.usmp_id)


# A flight "landing" in Sydney at 23:45 local, which is 12:45 UTC: before its 14:45 UTC take-off.
BACKWARDS = event(
    "flight",
    start="2025-02-16T14:45:00",
    time_zone="UTC",
    end="2025-02-16T23:45:00",
    end_time_zone="Australia/Sydney",
)
LONG = event("long", start="2026-03-02T09:00:00", end="2026-03-20T09:00:00")
NORMAL = [
    event("meeting", start="2026-03-02T09:00:00", end="2026-03-02T10:00:00", time_zone="UTC"),
    event("trip", start="2026-03-02", end="2026-04-30"),  # long all-day events are normal
    event("no-end", start="2026-03-02T09:00:00"),
    event(
        "real-flight",
        start="2026-03-02T09:00:00",
        time_zone="Europe/London",
        end="2026-03-02T12:00:00",
        end_time_zone="America/New_York",
    ),
    event("week", start="2026-03-02T09:00:00", end="2026-03-09T09:00:00"),
]


def test_odd_events_are_found_with_a_hint_but_no_content() -> None:
    found = warnings("calendar", [CAL, BACKWARDS, LONG, *NORMAL])

    assert [(w.usmp_id, w.field) for w in found] == [
        (BACKWARDS.usmp_id, "end"),
        (LONG.usmp_id, "end"),
    ]
    assert [(w.detail, w.hint) for w in found] == [
        ("ends 2 hours before it starts", "starts 2025-02-16T14:45:00 (UTC)"),
        ("lasts 18 days: check its end", "starts 2026-03-02T09:00:00"),
    ]


@pytest.mark.parametrize(
    ("end", "detail"),
    [
        ("2025-02-17T00:45:00", "ends 1 hour before it starts"),
        ("2025-02-17T01:15:00", "ends 0.5 hours before it starts"),
        ("2025-02-15T01:45:00", "ends 2 days before it starts"),
    ],
)
def test_how_far_back(end: str, detail: str) -> None:
    item = event(
        "x", start="2025-02-16T14:45:00", time_zone="UTC", end=end, end_time_zone="Australia/Sydney"
    )
    [found] = warnings("calendar", [item])
    assert found.detail == detail


def test_other_domains_have_no_checks() -> None:
    note = Item.create(
        connector="s", native_id="1", domain="notes", type="note", schema_version=1, payload={}
    )
    assert warnings("notes", [note]) == []


def test_event_ending_before_it_starts_is_moved_without_its_end(tmp_path: Path) -> None:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:x", "BEGIN:VEVENT", "UID:b@x",
        "DTSTAMP:20260101T000000Z", "DTSTART;TZID=Europe/Paris:20260302T090000",
        "DTEND;TZID=Europe/Paris:20260302T080000", "SUMMARY:Broken", "END:VEVENT",
        "END:VCALENDAR", "",
    ]  # fmt: skip
    (tmp_path / "in.ics").write_text("\r\n".join(lines))

    result = migrate(
        IcsConnector({"path": str(tmp_path / "in.ics")}),
        IcsConnector({"path": str(tmp_path / "out")}),
        bundle_path=tmp_path / "b.usmp",
    )

    assert result.report is not None
    assert result.report.ok, result.report.errors
    [loss] = [loss for loss in result.report.losses if loss.field == "end"]
    assert loss.reason is LossReason.VALUE_CONVERTED
    assert "ended before it started" in loss.detail
    assert "Broken" not in loss.detail


def test_warnings_reach_the_preview_and_every_report(tmp_path: Path) -> None:
    from usmp.connectors.memory import MemoryConnector

    source = MemoryConnector({"domain": "calendar"}, items=[CAL, BACKWARDS, LONG, *NORMAL])

    result = migrate(
        source, IcsConnector({"path": str(tmp_path / "out")}), bundle_path=tmp_path / "b.usmp"
    )

    assert len(result.preview.warnings) == 2
    report = result.report
    assert report is not None and report.ok, "warnings never make a migration fail"
    assert report.warnings == result.preview.warnings
    assert "Worth checking in the source" in to_markdown(report)
    assert "ends 2 hours before it starts" in to_markdown(report)
    assert "lasts 18 days" in to_html(report)
    assert "Worth checking" not in to_markdown(report.model_copy(update={"warnings": []}))
