"""``usmp migrate``: move one domain from a source to a target and verify it."""

import shlex
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from usmp.cli import wizard
from usmp.core.bundle import BundleError, BundleReader
from usmp.core.engine import MigrationError, MigrationResult, Preview, migrate, resume
from usmp.core.models import ItemStatus, Selection, VerificationReport
from usmp.core.progress import ProgressError, progress_path, read_progress
from usmp.core.report import (
    ALREADY_PRESENT,
    LEFT_OUT,
    REASON_LABELS,
    STATUS_LABELS,
    summarize_losses,
    write_reports,
)
from usmp.core.selection import SelectionError, make_selection
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
    console.print("  Backup of the source:")
    console.print(f"  {preview.bundle_path}", soft_wrap=True, markup=False, highlight=False)
    if preview.selection is not None:
        console.print(f"  Only:  {preview.selection.describe()}", markup=False, highlight=False)
        console.print(
            f"  [dim]{preview.left_out} item(s) left out (they stay in the backup).[/dim]"
        )
    counts = Table("Type", "Items", box=None, padding=(0, 2))
    for type_, count in sorted(preview.counts.items()):
        counts.add_row(type_, str(count))
    console.print(counts)
    _show_warnings(preview)
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


_SHOWN_WARNINGS = 5


def _show_warnings(preview: Preview) -> None:
    if not preview.warnings:
        return
    console.print(
        f"\n  [yellow]⚠ {len(preview.warnings)} item(s) look wrong in the source. They will be "
        "copied exactly as they are:[/yellow]"
    )
    for w in preview.warnings[:_SHOWN_WARNINGS]:
        where = f"{w.hint}: " if w.hint else ""
        console.print(f"    {where}{w.detail}", markup=False, highlight=False)
    more = len(preview.warnings) - _SHOWN_WARNINGS
    if more > 0:
        console.print(f"    [dim]… and {more} more, all listed in the report.[/dim]")


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
    if s.already_present:
        console.print(f"  [dim]{s.already_present} {ALREADY_PRESENT}[/dim]")
    if s.left_out:
        console.print(f"  [dim]{s.left_out} {LEFT_OUT}[/dim]")
    for error in report.errors:
        console.print(f"  [red]• {error}[/red]")
    html = next((p for p in paths if p.suffix == ".html"), paths[0])
    others = ", ".join(p.suffix.lstrip(".").upper() for p in paths if p != html)
    console.print("  Full report (open in a browser):")
    console.print(f"  {html}", soft_wrap=True, markup=False, highlight=False)
    if others:
        console.print(f"  [dim]Also saved as {others} next to it.[/dim]")


def command(
    source: Annotated[
        str | None,
        typer.Option(
            "--from", "-f", help="Source connector, e.g. ics:path=old.ics. Omit both to be guided."
        ),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--to", "-t", help="Target connector, e.g. caldav:url=…,username=…"),
    ] = None,
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
    since: Annotated[
        str | None,
        typer.Option(
            help="Only move events that end on or after this date (YYYY-MM-DD) or time "
            "(YYYY-MM-DDTHH:MM). Recurring events are kept whole.",
        ),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(
            help="Only move events that start on or before this date (the whole day counts), or "
            "before this time.",
        ),
    ] = None,
    calendars: Annotated[
        list[str] | None,
        typer.Option(
            "--calendar",
            help="Only move this calendar (by name, ignoring case). Repeat for several.",
        ),
    ] = None,
    resume_from: Annotated[
        Path | None,
        typer.Option(
            "--resume",
            help="Finish an interrupted migration from its backup (.usmp). The source is not "
            "read again; nothing already written is written twice.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Migrate data from a source to a target, then verify that it arrived intact.

    Run it without --from and --to to be guided step by step. The source is only ever read. A
    backup bundle of everything exported is saved first. Running the same migration again, or
    with --resume after it was interrupted, never writes anything twice. --since, --until and
    --calendar move only part of it; everything is still saved in the backup.
    """
    selection = _selection(since, until, calendars or [])
    if resume_from is not None:
        _resume(
            resume_from,
            target,
            domain,
            report_dir,
            dry_run=dry_run,
            yes=yes,
            source=source,
            selection=selection,
        )
        return
    repeat: str | None = None
    if source is None and target is None:
        if not _interactive():
            console.print(
                "[red]✗ Say what to migrate with --from and --to, or run `usmp migrate` in a "
                "terminal to be guided.[/red]"
            )
            raise typer.Exit(EXIT_ERROR)
        try:
            chosen_source, chosen_target = wizard.run()
        except typer.Abort:
            console.print("\nCancelled: nothing was written.")
            raise typer.Exit(EXIT_CANCELLED) from None
        except ConnectorError as exc:
            console.print(f"[red]✗ {exc}[/red]")
            raise typer.Exit(EXIT_ERROR) from exc
        source, target = chosen_source.spec, chosen_target.spec
        domain = domain or "calendar"
        if selection is None:
            try:
                since, until = wizard.date_range()
            except typer.Abort:
                console.print("\nCancelled: nothing was written.")
                raise typer.Exit(EXIT_CANCELLED) from None
            selection = _selection(since, until, calendars or [])
        repeat = wizard.command_line(
            chosen_source, chosen_target, since=since, until=until, calendars=calendars or []
        )
        endpoints = (chosen_source, chosen_target)
    elif source is None or target is None:
        missing = "--from" if source is None else "--to"
        console.print(f"[red]✗ Missing {missing}. Give both --from and --to, or neither.[/red]")
        raise typer.Exit(EXIT_ERROR)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    bundle_path = bundle or Path(f"usmp-{domain or 'export'}-{stamp}.usmp")
    try:
        if repeat is not None:
            src, dst = (registry.get(e.name)(e.config) for e in endpoints)
        else:
            src, dst = _connector(source, domain), _connector(target, domain)
        result = migrate(
            src,
            dst,
            domain=domain,
            bundle_path=bundle_path,
            dry_run=dry_run,
            confirm=_confirmer(dry_run=dry_run, yes=yes),
            progress=_progress,
            selection=selection,
        )
    except (ConnectorError, MigrationError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        console.print("  Nothing was written to the target.")
        raise typer.Exit(EXIT_ERROR) from exc
    except typer.Abort:
        console.print("\nCancelled: nothing was written to the target.")
        raise typer.Exit(EXIT_CANCELLED) from None
    except KeyboardInterrupt:
        _stopped(bundle_path)
    _finish(result, report_dir, dry_run=dry_run, repeat=repeat)


def _selection(since: str | None, until: str | None, calendars: list[str]) -> Selection | None:
    try:
        return make_selection(since, until, calendars)
    except SelectionError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(EXIT_ERROR) from None


def _progress(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")


def _confirmer(*, dry_run: bool, yes: bool) -> Callable[[Preview], bool]:
    def confirm(preview: Preview) -> bool:
        _show_preview(preview)
        if dry_run or yes:
            return True
        if not _interactive():
            console.print("[red]Not running interactively: pass --yes to confirm.[/red]")
            return False
        return typer.confirm(f"\nWrite {preview.total} items to the target?", default=False)

    return confirm


def _resume_command(bundle_path: Path) -> str:
    return f"usmp migrate --resume {shlex.quote(str(bundle_path))}"


def _stopped(bundle_path: Path) -> NoReturn:
    if progress_path(bundle_path).exists():
        console.print(
            "\n[yellow]Stopped.[/yellow] Everything written so far is recorded. To finish:"
        )
        console.print(f"  {_resume_command(bundle_path)}", soft_wrap=True, markup=False)
    else:
        console.print("\nStopped: nothing was written to the target.")
    raise typer.Exit(EXIT_CANCELLED)


def _resume(
    bundle_path: Path,
    target: str | None,
    domain: str | None,
    report_dir: Path,
    *,
    dry_run: bool,
    yes: bool,
    source: str | None,
    selection: Selection | None,
) -> None:
    if source is not None:
        console.print("[red]✗ --resume reads from the backup: leave out --from.[/red]")
        raise typer.Exit(EXIT_ERROR)
    try:
        record = read_progress(bundle_path)
        if domain is None:
            domain = record[0].domain if record else _bundle_domain(bundle_path)
        if target is not None:
            dst = _connector(target, domain)
        elif record is not None:
            header = record[0]
            dst = registry.get(header.target_connector)(header.target_config)
        else:
            console.print(
                f"[red]✗ No progress record next to {bundle_path}: give --to to import this "
                "backup into a target.[/red]"
            )
            raise typer.Exit(EXIT_ERROR)
        result = resume(
            dst,
            bundle_path=bundle_path,
            dry_run=dry_run,
            confirm=_confirmer(dry_run=dry_run, yes=yes),
            progress=_progress,
            selection=selection,
        )
    except (ConnectorError, MigrationError, ProgressError, BundleError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        console.print("  Nothing more was written to the target.")
        raise typer.Exit(EXIT_ERROR) from exc
    except typer.Abort:
        console.print("\nCancelled: nothing more was written to the target.")
        raise typer.Exit(EXIT_CANCELLED) from None
    except KeyboardInterrupt:
        _stopped(bundle_path)
    _finish(result, report_dir, dry_run=dry_run, repeat=None)


def _bundle_domain(bundle_path: Path) -> str | None:
    with BundleReader(bundle_path) as bundle:
        domains = {c.domain for c in bundle.manifest.counts}
    return domains.pop() if len(domains) == 1 else None


def _finish(
    result: MigrationResult, report_dir: Path, *, dry_run: bool, repeat: str | None
) -> None:
    if dry_run:
        _show_preview(result.preview)
        console.print("\n[bold]Dry run:[/bold] nothing was written to the target.")
        return
    if result.cancelled or result.report is None:
        console.print("Cancelled: nothing was written to the target.")
        raise typer.Exit(EXIT_CANCELLED)
    paths = write_reports(result.report, report_dir)
    _show_report(result.report, paths)
    if result.report.errors:
        console.print("\n  Fix the problem above, then finish without writing anything twice:")
        console.print(
            f"  {_resume_command(result.preview.bundle_path)}", soft_wrap=True, markup=False
        )
    if repeat is not None:
        console.print("\n  To run the same migration again without questions:")
        console.print(f"  {repeat}", soft_wrap=True, markup=False, highlight=False)
    raise typer.Exit(EXIT_OK if result.report.ok else EXIT_NOT_VERIFIED)
