"""Render verification reports and previews as JSON, Markdown and HTML, and undo reports."""

import html
from collections import Counter
from pathlib import Path

from usmp.core.models import (
    ItemStatus,
    LossRecord,
    UndoOutcome,
    UndoReport,
    VerificationReport,
)

STATUS_LABELS = {
    ItemStatus.VERIFIED: "Verified",
    ItemStatus.VERIFIED_WITH_DECLARED_LOSS: "Verified (declared losses)",
    ItemStatus.MISMATCH: "Mismatch",
    ItemStatus.MISSING: "Missing",
}

ALREADY_PRESENT = (
    "item(s) were already in the target from an earlier run, so they were not written again "
    "(they are still counted and verified above)."
)

WARNINGS_NOTE = (
    "These items look wrong in the source, for example an event that ends before it starts. "
    "They were migrated exactly as they are; fix them at either end if they are mistakes."
)
LEFT_OUT = (
    "item(s) were outside the chosen selection: they are in the backup but were not migrated, "
    "and are not counted above."
)

REASON_LABELS = {
    "unsupported_by_canonical": "USMP has no place for it yet (kept in the backup)",
    "unsupported_by_target": "the target cannot store it",
    "not_readable_by_target": "stored, but the target cannot read it back to verify",
    "value_converted": "kept, but converted",
}


def summarize_losses(losses: list[LossRecord]) -> list[tuple[str, str, str, int]]:
    """Group losses into ``(field, reason, severity, item count)`` rows, most severe first."""
    order = {"error": 0, "warning": 1, "info": 2}
    counts = Counter((loss.field, loss.reason.value, loss.severity.value) for loss in losses)
    rows = [(f, r, s, n) for (f, r, s), n in counts.items()]
    return sorted(rows, key=lambda row: (order[row[2]], row[0], row[1]))


def _field_label(name: str) -> str:
    return "whole item" if name == "*" else name


def to_markdown(report: VerificationReport) -> str:
    s = report.summary
    verdict = "✅ Migration verified" if report.ok else "❌ Migration NOT fully verified"
    lines = [
        f"# USMP migration report: {report.domain}",
        "",
        f"**{verdict}**",
        "",
        "| | |",
        "|---|---|",
        f"| From | {report.source.display_name}"
        + (f" ({report.source.label})" if report.source.label else "")
        + " |",
        f"| To | {report.target.display_name}"
        + (f" ({report.target.label})" if report.target.label else "")
        + " |",
        f"| Started | {report.started_at:%Y-%m-%d %H:%M:%S %Z} |",
        f"| Finished | {report.finished_at:%Y-%m-%d %H:%M:%S %Z} |",
        f"| Run id | `{report.run_id}` |",
        *([f"| Selection | {report.selection.describe()} |"] if report.selection else []),
        "",
        "## Summary",
        "",
        "| Status | Items |",
        "|---|---:|",
        f"| {STATUS_LABELS[ItemStatus.VERIFIED]} | {s.verified} |",
        f"| {STATUS_LABELS[ItemStatus.VERIFIED_WITH_DECLARED_LOSS]} "
        f"| {s.verified_with_declared_loss} |",
        f"| {STATUS_LABELS[ItemStatus.MISMATCH]} | {s.mismatch} |",
        f"| {STATUS_LABELS[ItemStatus.MISSING]} | {s.missing} |",
        f"| **Total** | **{s.total}** |",
    ]
    if s.already_present:
        lines += ["", f"{s.already_present} {ALREADY_PRESENT}"]
    if s.left_out:
        lines += ["", f"{s.left_out} {LEFT_OUT}"]
    if report.errors:
        lines += ["", "## Errors", "", *(f"- {e}" for e in report.errors)]
    if report.warnings:
        lines += ["", "## Worth checking in the source", "", WARNINGS_NOTE, ""]
        lines += ["| Item | Where | What |", "|---|---|---|"]
        for w in report.warnings:
            lines.append(f"| `{w.usmp_id}` | {w.hint or '-'} | {w.detail} |")
    problems = [i for i in report.items if i.status in (ItemStatus.MISMATCH, ItemStatus.MISSING)]
    if problems:
        lines += ["", "## Items that need attention", "", "| Item | Type | Status | Fields |"]
        lines += ["|---|---|---|---|"]
        for item in problems:
            fields = ", ".join(item.mismatched_fields) or "-"
            lines.append(
                f"| `{item.usmp_id}` | {item.type} | {STATUS_LABELS[item.status]} | {fields} |"
            )
    if report.losses:
        lines += ["", "## Declared losses", "", "| Field | Why | Severity | Items |"]
        lines += ["|---|---|---|---:|"]
        for name, reason, severity, count in summarize_losses(report.losses):
            why = REASON_LABELS.get(reason, reason)
            lines.append(f"| {_field_label(name)} | {why} | {severity} | {count} |")
    return "\n".join(lines) + "\n"


_CSS = """
:root { --bg:#fff; --fg:#1b1f24; --muted:#59636e; --line:#d1d9e0;
        --ok:#1a7f37; --bad:#cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --fg:#e6edf3; --muted:#9198a1; --line:#3d444d;
          --ok:#3fb950; --bad:#f85149; }
}
body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,sans-serif;
       max-width:960px; margin:0 auto; padding:24px 16px; }
h1 { font-size:1.5rem; } h2 { font-size:1.15rem; margin-top:2rem; }
.verdict { font-size:1.1rem; font-weight:600; padding:12px 16px; border-radius:8px;
           border:1px solid var(--line); }
.ok { color:var(--ok); } .bad { color:var(--bad); }
table { border-collapse:collapse; width:100%; margin-top:8px; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
code { font-size:.85em; } .muted { color:var(--muted); }
.wrap { overflow-x:auto; }
"""


def to_html(report: VerificationReport) -> str:
    e = html.escape
    s = report.summary

    def row(*cells: str, num: int | None = None) -> str:
        tds = "".join(
            f'<td class="num">{c}</td>' if i == num else f"<td>{c}</td>"
            for i, c in enumerate(cells)
        )
        return f"<tr>{tds}</tr>"

    def who(info_name: str, label: str | None) -> str:
        return e(info_name) + (f' <span class="muted">({e(label)})</span>' if label else "")

    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>USMP report: {e(report.domain)}</title><style>{_CSS}</style></head><body>",
        f"<h1>USMP migration report: {e(report.domain)}</h1>",
        f'<p class="verdict {"ok" if report.ok else "bad"}">'
        + ("✅ Migration verified" if report.ok else "❌ Migration NOT fully verified")
        + "</p>",
        "<table>",
        row("From", who(report.source.display_name, report.source.label)),
        row("To", who(report.target.display_name, report.target.label)),
        row("Started", e(f"{report.started_at:%Y-%m-%d %H:%M:%S %Z}")),
        row("Finished", e(f"{report.finished_at:%Y-%m-%d %H:%M:%S %Z}")),
        row("Run id", f"<code>{e(report.run_id)}</code>"),
        *([row("Selection", e(report.selection.describe()))] if report.selection else []),
        "</table><h2>Summary</h2><table>",
        "<tr><th>Status</th><th class='num'>Items</th></tr>",
        row(STATUS_LABELS[ItemStatus.VERIFIED], str(s.verified), num=1),
        row(
            STATUS_LABELS[ItemStatus.VERIFIED_WITH_DECLARED_LOSS],
            str(s.verified_with_declared_loss),
            num=1,
        ),
        row(STATUS_LABELS[ItemStatus.MISMATCH], str(s.mismatch), num=1),
        row(STATUS_LABELS[ItemStatus.MISSING], str(s.missing), num=1),
        row("<strong>Total</strong>", f"<strong>{s.total}</strong>", num=1),
        "</table>",
    ]
    if s.already_present:
        parts.append(f"<p>{s.already_present} {e(ALREADY_PRESENT)}</p>")
    if s.left_out:
        parts.append(f"<p>{s.left_out} {e(LEFT_OUT)}</p>")
    if report.errors:
        parts.append("<h2>Errors</h2><ul>")
        parts += [f"<li class='bad'>{e(err)}</li>" for err in report.errors]
        parts.append("</ul>")
    if report.warnings:
        parts.append(f"<h2>Worth checking in the source</h2><p>{e(WARNINGS_NOTE)}</p>")
        parts.append("<div class='wrap'><table><tr><th>Item</th><th>Where</th><th>What</th></tr>")
        parts += [
            row(f"<code>{e(w.usmp_id)}</code>", e(w.hint or "-"), e(w.detail))
            for w in report.warnings
        ]
        parts.append("</table></div>")
    problems = [i for i in report.items if i.status in (ItemStatus.MISMATCH, ItemStatus.MISSING)]
    if problems:
        parts.append("<h2>Items that need attention</h2><div class='wrap'><table>")
        parts.append("<tr><th>Item</th><th>Type</th><th>Status</th><th>Fields</th></tr>")
        for item in problems:
            parts.append(
                row(
                    f"<code>{e(item.usmp_id)}</code>",
                    e(item.type),
                    e(STATUS_LABELS[item.status]),
                    e(", ".join(item.mismatched_fields) or "-"),
                )
            )
        parts.append("</table></div>")
    if report.losses:
        parts.append("<h2>Declared losses</h2><div class='wrap'><table>")
        parts.append(
            "<tr><th>Field</th><th>Why</th><th>Severity</th><th class='num'>Items</th></tr>"
        )
        for name, reason, severity, count in summarize_losses(report.losses):
            parts.append(
                row(
                    e(_field_label(name)),
                    e(REASON_LABELS.get(reason, reason)),
                    e(severity),
                    str(count),
                    num=3,
                )
            )
        parts.append("</table></div>")
    parts.append("</body></html>\n")
    return "\n".join(parts)


def write_reports(report: VerificationReport, directory: Path) -> list[Path]:
    """Write ``report`` as JSON, Markdown and HTML into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / f"usmp-report-{report.started_at:%Y%m%d-%H%M%S}-{report.run_id[:8]}"
    outputs = {
        stem.with_suffix(".json"): report.model_dump_json(indent=2) + "\n",
        stem.with_suffix(".md"): to_markdown(report),
        stem.with_suffix(".html"): to_html(report),
    }
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
    return list(outputs)


UNDO_LABELS = {
    UndoOutcome.REMOVED: "Removed",
    UndoOutcome.ALREADY_GONE: "Already gone",
    UndoOutcome.KEPT: "Kept",
}
LEFT_ALONE = "item(s) were already on the target before the migration, so undo never touched them."


def undo_markdown(report: UndoReport) -> str:
    """The undo report as Markdown: totals, then every kept item with the reason."""
    target = report.target.display_name + (
        f" ({report.target.label})" if report.target.label else ""
    )
    lines = [
        f"# USMP undo report: {report.domain}",
        "",
        f"- **Target:** {target}",
        f"- **Migration from:** {report.migration_source.display_name}",
        f"- **Run:** `{report.run_id}` ({report.started_at:%Y-%m-%d %H:%M:%S} UTC)",
        f"- **Result:** {'finished' if report.ok else 'stopped early'}",
        "",
        "| Outcome | Items |",
        "|---|---|",
    ]
    counts = Counter(r.outcome for r in report.items)
    lines += [f"| {label} | {counts.get(outcome, 0)} |" for outcome, label in UNDO_LABELS.items()]
    if report.summary.left_alone:
        lines += ["", f"{report.summary.left_alone} {LEFT_ALONE}"]
    kept = [r for r in report.items if r.outcome is UndoOutcome.KEPT]
    if kept:
        lines += ["", "## Kept", "", "| Item | Type | Why |", "|---|---|---|"]
        lines += [f"| `{r.target_native_id}` | {r.type} | {r.reason or ''} |" for r in kept]
    if report.errors:
        lines += ["", "## Errors", ""] + [f"- {e}" for e in report.errors]
    return "\n".join(lines) + "\n"


def write_undo_report(report: UndoReport, directory: Path) -> list[Path]:
    """Write ``report`` as JSON and Markdown into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / f"usmp-undo-{report.started_at:%Y%m%d-%H%M%S}-{report.run_id[:8]}"
    outputs = {
        stem.with_suffix(".json"): report.model_dump_json(indent=2) + "\n",
        stem.with_suffix(".md"): undo_markdown(report),
    }
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
    return list(outputs)
