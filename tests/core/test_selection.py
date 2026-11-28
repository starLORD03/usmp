from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import make_capabilities, make_event
from usmp.connectors.ics import IcsConnector
from usmp.connectors.memory import MemoryConnector
from usmp.core.bundle import BundleReader
from usmp.core.engine import MigrationError, migrate, resume
from usmp.core.models import Item, Selection, VerificationReport
from usmp.core.progress import progress_path, read_progress
from usmp.core.report import to_html
from usmp.domains.calendar import calendar_item, event_item

UTC1 = timezone(timedelta(hours=1))
SINCE_2026 = Selection(since=datetime(2026, 1, 1, tzinfo=UTC1))
WORK_ONLY = Selection(collections=["Work"])


def calendar_source(tmp_path: Path) -> IcsConnector:
    work = calendar_item("s", "work", {"name": "Work"})
    home = calendar_item("s", "home", {"name": "Home"})
    items: list[Item] = [work, home]
    for n, start in enumerate(["2025-03-01", "2026-03-01", "2026-04-01"]):
        items.append(
            event_item("s", f"w{n}", {"uid": f"w{n}@x", "start": start}, parent=work.usmp_id)
        )
    items.append(event_item("s", "h", {"uid": "h@x", "start": "2025-05-01"}, parent=home.usmp_id))
    folder = tmp_path / "source"
    list(IcsConnector({"path": str(folder)}).open_writer().write(items))
    return IcsConnector({"path": str(folder)})


def event_uids(folder: Path) -> set[str]:
    records = IcsConnector({"path": str(folder)}).open_reader().read("calendar")
    return {str(r.item.payload["uid"]) for r in records if r.item.type == "event"}


def test_only_the_selection_is_written_but_everything_is_backed_up(tmp_path: Path) -> None:
    out = tmp_path / "out"

    result = migrate(
        calendar_source(tmp_path),
        IcsConnector({"path": str(out)}),
        bundle_path=tmp_path / "b.usmp",
        selection=SINCE_2026,
    )

    report = result.report
    assert report is not None and report.ok, report
    assert report.selection == SINCE_2026
    assert report.summary.total == 3  # Work and its two 2026 events
    assert report.summary.left_out == 3  # Home, its event and the 2025 event
    assert result.preview.left_out == 3
    assert result.preview.counts == {"calendar": 1, "event": 2}
    assert event_uids(out) == {"w1@x", "w2@x"}
    with BundleReader(tmp_path / "b.usmp") as bundle:
        assert len(list(bundle.iter_items())) == 6


def test_resume_keeps_the_selection_it_started_with(tmp_path: Path) -> None:
    bundle = tmp_path / "b.usmp"
    out = tmp_path / "out"
    migrate(
        calendar_source(tmp_path),
        IcsConnector({"path": str(out)}),
        bundle_path=bundle,
        selection=WORK_ONLY,
        confirm=lambda _p: True,
    )
    record = read_progress(bundle)
    assert record is not None and record[0].selection == WORK_ONLY

    with pytest.raises(MigrationError, match=r"different selection \(calendars: Work\)"):
        resume(IcsConnector({"path": str(out)}), bundle_path=bundle, selection=SINCE_2026)

    again = resume(IcsConnector({"path": str(out)}), bundle_path=bundle)
    assert again.report is not None and again.report.ok
    assert again.report.selection == WORK_ONLY
    assert again.report.summary.already_present == 4
    assert again.report.summary.left_out == 2


def test_resume_without_a_record_can_import_part_of_a_backup(tmp_path: Path) -> None:
    bundle = tmp_path / "b.usmp"
    migrate(
        calendar_source(tmp_path),
        IcsConnector({"path": str(tmp_path / "x")}),
        bundle_path=bundle,
        dry_run=True,
    )
    assert not progress_path(bundle).exists()

    result = resume(
        IcsConnector({"path": str(tmp_path / "part")}), bundle_path=bundle, selection=WORK_ONLY
    )

    assert result.report is not None and result.report.ok
    assert event_uids(tmp_path / "part") == {"w0@x", "w1@x", "w2@x"}


def test_a_selection_matching_nothing_stops_before_writing(tmp_path: Path) -> None:
    far = Selection(since=datetime(2040, 1, 1, tzinfo=UTC1))
    with pytest.raises(MigrationError, match="nothing matches the selection"):
        migrate(
            calendar_source(tmp_path),
            IcsConnector({"path": str(tmp_path / "out")}),
            bundle_path=tmp_path / "b.usmp",
            selection=far,
        )
    assert not (tmp_path / "out").exists()


def test_domains_that_cannot_select_are_refused(tmp_path: Path) -> None:
    src = MemoryConnector(capabilities=make_capabilities(), items=[make_event("1")])
    with pytest.raises(MigrationError, match="cannot choose part of 'demo' data"):
        migrate(
            src,
            MemoryConnector(capabilities=make_capabilities()),
            bundle_path=tmp_path / "b.usmp",
            selection=SINCE_2026,
        )


def test_reports_show_the_selection(tmp_path: Path) -> None:
    result = migrate(
        calendar_source(tmp_path),
        IcsConnector({"path": str(tmp_path / "out")}),
        bundle_path=tmp_path / "b.usmp",
        selection=WORK_ONLY,
    )
    assert result.report is not None
    page = to_html(result.report)
    assert "<td>Selection</td><td>calendars: Work</td>" in page
    assert "2 item(s) were outside the chosen selection" in page

    data = result.report.model_dump(mode="json")
    data["selection"] = None
    with pytest.raises(ValidationError, match="only be left out by a selection"):
        VerificationReport.model_validate(data)
