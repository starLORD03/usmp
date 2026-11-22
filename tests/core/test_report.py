from pathlib import Path

from tests.conftest import make_capabilities, make_event
from usmp.connectors.memory import MemoryConnector
from usmp.core.engine import migrate
from usmp.core.models import VerificationReport
from usmp.core.report import summarize_losses, to_html, to_markdown, write_reports


def lossy_report(tmp_path: Path) -> VerificationReport:
    source = MemoryConnector(
        {"label": "Old <calendar>"},
        capabilities=make_capabilities(),
        items=[make_event(str(n), title="T", start="S", location="L") for n in range(2)],
    )
    target = MemoryConnector(capabilities=make_capabilities(write=["title", "start"]))
    report = migrate(source, target, bundle_path=tmp_path / "b.usmp").report
    assert report is not None
    return report


def test_markdown_summarizes_and_lists_losses(tmp_path: Path) -> None:
    md = to_markdown(lossy_report(tmp_path))

    assert "✅ Migration verified" in md
    assert "| Verified (declared losses) | 2 |" in md
    assert "| location | the target cannot store it | warning | 2 |" in md


def test_html_escapes_user_content(tmp_path: Path) -> None:
    out = to_html(lossy_report(tmp_path))

    assert "Old &lt;calendar&gt;" in out
    assert "<calendar>" not in out
    assert out.startswith("<!doctype html>")


def test_problems_and_errors_are_rendered(tmp_path: Path) -> None:
    report = lossy_report(tmp_path)
    item = report.items[0].model_copy(update={"status": "missing"})
    broken = report.model_copy(
        update={
            "items": [item],
            "summary": report.summary.model_copy(
                update={"total": 1, "verified_with_declared_loss": 0, "missing": 1}
            ),
            "errors": ["writing stopped"],
        }
    )

    md, page = to_markdown(broken), to_html(broken)

    assert "❌" in md and "writing stopped" in md and "Items that need attention" in md
    assert "writing stopped" in page and "Missing" in page


def test_write_reports_creates_three_files(tmp_path: Path) -> None:
    paths = write_reports(lossy_report(tmp_path), tmp_path / "reports")

    assert sorted(p.suffix for p in paths) == [".html", ".json", ".md"]
    assert VerificationReport.model_validate_json(paths[0].read_text()).ok


def test_summarize_losses_orders_by_severity(tmp_path: Path) -> None:
    rows = summarize_losses(lossy_report(tmp_path).losses)

    assert rows == [("location", "unsupported_by_target", "warning", 2)]
