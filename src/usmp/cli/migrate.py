"""``usmp migrate``: move one domain from a source to a target and verify it."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from usmp.core.engine import MigrationError, Preview, migrate
from usmp.core.models import ItemStatus, VerificationReport
from usmp.core.report import REASON_LABELS, STATUS_LABELS, summarize_losses, write_reports
from usmp.sdk import registry
from usmp.sdk.connector import Connector, ConnectorError

console = Console()

EXIT_OK, EXIT_NOT_VERIFIED, EXIT_ERROR, EXIT_CANCELLED = 0, 1, 2, 3


def _interactive() -> bool:
    return sys.stdin.isatty()


def _connector(spec: str, domain: str | None) -> Connector:
    name, config = registry.parse_spec(spec)
    cls = registry.get(name)
    if domain and "domain" in cls.config_keys and "domain" not in config:
        config["domain"] = domain
    return cls(config)


def _show_preview(preview: Preview) -> None:
    src = preview.source.display_name + (
        f" ({preview.source.label})" if preview.source.label else ""
    )
    dst = preview.target.display_name + (
        f" ({preview.target.label})" if preview.target.label else ""
    )
    console.print(f"\n[bold]Migration preview: {preview.domain}[/bold]")
    console.print(f"  From:  {src}")
    console.print(f"  To:    {dst}")
    console.print(f"  Backup of the source: {preview.bundle_path}")
    counts = Table("Type", "Items", box=None, padding=(0, 2))
    for type_, count in sorted(preview.counts.items()):
        counts.add_row(type_, str(count))
    console.print(counts)
    if not preview.losses:
        console.print(
            "  [green]Nothing will be lost: every field can be written and verified.[/green]"
        )
        return
    console.print("\n  [yellow]Some data cannot be carried over:[/yellow]")
    losses = Table("Field", "Why", "Items", box=None, padding=(0, 2))
    for name, reason, severity, count in summarize_losses(preview.losses):
        style = {"error": "red", "warning": "yellow"}.get(severity, "")
        field = "whole item" if name == "*" else name
        losses.add_row(
            f"[{style}]{field}[/{style}]" if style else field, REASON_LABELS[reason], str(count)
        )
    console.print(losses)
    if preview.blocked_items:
        console.print(
            f"  [red]{preview.blocked_items} item(s) cannot be stored by the target at all "
            "and will be reported as missing.[/red]"
        )


def _show_report(report: VerificationReport, paths: list[Path]) -> None:
    s = report.summary
    style = "green" if report.ok else "red"
    verdict = "Migration verified" if report.ok else "Migration NOT fully verified"
    console.print(f"\n[bold {style}]{'✓' if report.ok else '✗'} {verdict}[/bold {style}]")
    table = Table("Status", "Items", box=None, padding=(0, 2))
    for status, count in (
        (ItemStatus.VERIFIED, s.verified),
        (ItemStatus.VERIFIED_WITH_DECLARED_LOSS, s.verified_with_declared_loss),
        (ItemStatus.MISMATCH, s.mismatch),
        (ItemStatus.MISSING, s.missing),
    ):
        table.add_row(STATUS_LABELS[status], str(count))
    console.print(table)
    for error in report.errors:
        console.print(f"  [red]• {error}[/red]")
    console.print("  Reports: " + ", ".join(str(p) for p in paths))


def command(
    source: Annotated[
        str, typer.Option("--from", "-f", help="Source connector, e.g. jsondir:path=./old")
    ],
    target: Annotated[
        str, typer.Option("--to", "-t", help="Target connector, e.g. jsondir:path=./new")
    ],
    domain: Annotated[
        str | None,
        typer.Option(help="Domain to migrate, e.g. calendar. Optional if the source has one."),
    ] = None,
    bundle: Annotated[
        Path | None,
        typer.Option(help="Where to save the backup bundle. Default: ./usmp-<domain>-<time>.usmp"),
    ] = None,
    report_dir: Annotated[
        Path, typer.Option(help="Directory for the JSON, Markdown and HTML reports.")
    ] = Path("usmp-reports"),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Export and preview only; write nothing to the target."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Migrate data from a source to a target, then verify that it arrived intact.

    The source is only ever read. A backup bundle of everything exported is saved first.
    """
    try:
        src, dst = _connector(source, domain), _connector(target, domain)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        bundle_path = bundle or Path(f"usmp-{domain or 'export'}-{stamp}.usmp")

        def confirm(preview: Preview) -> bool:
            _show_preview(preview)
            if dry_run or yes:
                return True
            if not _interactive():
                console.print("[red]Not running interactively: pass --yes to confirm.[/red]")
                return False
            return typer.confirm(f"\nWrite {preview.total} items to the target?", default=False)

        result = migrate(
            src,
            dst,
            domain=domain,
            bundle_path=bundle_path,
            dry_run=dry_run,
            confirm=confirm,
            progress=lambda message: console.print(f"[dim]{message}[/dim]"),
        )
    except (ConnectorError, MigrationError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(EXIT_ERROR) from exc

    if dry_run:
        _show_preview(result.preview)
        console.print("\n[bold]Dry run:[/bold] nothing was written to the target.")
        return
    if result.cancelled or result.report is None:
        console.print("Cancelled: nothing was written to the target.")
        raise typer.Exit(EXIT_CANCELLED)
    paths = write_reports(result.report, report_dir)
    _show_report(result.report, paths)
    raise typer.Exit(EXIT_OK if result.report.ok else EXIT_NOT_VERIFIED)
