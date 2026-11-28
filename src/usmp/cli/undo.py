"""``usmp undo``: remove what a migration created on its target, and nothing else."""

from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from usmp.cli.migrate import (
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_NOT_VERIFIED,
    EXIT_OK,
    _connector,
    _interactive,
    _progress,
    console,
)
from usmp.core.bundle import BundleError
from usmp.core.models import UndoOutcome, UndoReport
from usmp.core.progress import ProgressError, read_progress
from usmp.core.report import LEFT_ALONE, UNDO_LABELS, write_undo_report
from usmp.core.undo import UndoError, UndoPlan, undo
from usmp.sdk import registry
from usmp.sdk.connector import Connector, ConnectorError

SHOWN = 5  # kept items listed on screen; the report lists them all


def _show_plan(plan: UndoPlan) -> None:
    dst = plan.target.display_name + (f" ({plan.target.label})" if plan.target.label else "")
    console.print(f"\n[bold]Undo preview: {plan.domain}[/bold]")
    console.print(f"  Target:  {dst}")
    console.print(f"  Migration from:  {plan.migration_source.display_name}")
    counts = Table("Will be", "Items", box=None, padding=(0, 2))
    by_type = Counter(r.type for r in plan.removals)
    removed = ", ".join(f"{n} {t}" for t, n in sorted(by_type.items()))
    counts.add_row("Removed", f"{len(plan.removals)}" + (f" ({removed})" if removed else ""))
    counts.add_row("Kept", str(len(plan.kept)))
    counts.add_row("Already gone", str(len(plan.gone)))
    console.print(counts)
    if plan.left_alone:
        console.print(f"  [dim]{plan.left_alone} {LEFT_ALONE}[/dim]")
    for item in plan.kept[:SHOWN]:
        console.print(f"  • kept {item.type} {item.target_native_id}: {item.reason}",
                      markup=False, highlight=False, soft_wrap=True)  # fmt: skip
    if len(plan.kept) > SHOWN:
        console.print(f"  … and {len(plan.kept) - SHOWN} more, all listed in the report.")
    console.print(
        "  Containers (such as calendars) are only removed if they are empty afterwards. "
        "The backup still holds the originals."
    )


def _show_report(report: UndoReport, paths: list[Path]) -> None:
    if report.ok:
        console.print("\n[green]✓ Undo finished[/green]")
    else:
        console.print("\n[red]✗ Undo stopped early[/red]")
    table = Table("Outcome", "Items", box=None, padding=(0, 2))
    counts = Counter(r.outcome for r in report.items)
    for outcome, label in UNDO_LABELS.items():
        table.add_row(label, str(counts.get(outcome, 0)))
    console.print(table)
    for error in report.errors:
        console.print(f"  • {error}", markup=False, highlight=False, soft_wrap=True)
    if counts.get(UndoOutcome.KEPT):
        console.print("  Kept items were changed after the migration or are not USMP's alone;")
        console.print("  the report says why for each. Remove them yourself if you want them gone.")
    console.print("  Report:")
    console.print(f"  {paths[-1]}", soft_wrap=True, markup=False, highlight=False)


def _target(bundle: Path, spec: str | None) -> Connector:
    record = read_progress(bundle)
    if spec is not None:
        return _connector(spec, record[0].domain if record else None)
    if record is None:
        raise UndoError(
            f"there is no record of what this migration wrote (next to {bundle}), so it cannot "
            "be undone safely"
        )
    header = record[0]
    return registry.get(header.target_connector)(header.target_config)


def command(
    bundle: Annotated[
        Path,
        typer.Argument(
            help="The backup (.usmp) of the migration to undo. Its progress record must be next "
            "to it.",
            exists=True,
            dir_okay=False,
        ),
    ],
    target: Annotated[
        str | None,
        typer.Option(
            "--to",
            "-t",
            help="The migration's target, if it needs different sign-in options. Default: as "
            "recorded.",
        ),
    ] = None,
    report_dir: Annotated[
        Path, typer.Option(help="Directory for the JSON and Markdown undo report.")
    ] = Path("usmp-reports"),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be removed; remove nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Undo a migration: remove from its target only what it created.

    Items that were there before the migration, items changed since, and calendars that still
    hold anything are left alone and listed. The source is never touched.
    """

    def confirm(plan: UndoPlan) -> bool:
        _show_plan(plan)
        if yes:
            return True
        if not _interactive():
            console.print("[red]Not running interactively: pass --yes to confirm.[/red]")
            return False
        return typer.confirm(
            f"\nRemove {len(plan.removals)} items from the target? This cannot be reversed.",
            default=False,
        )

    try:
        result = undo(
            _target(bundle, target),
            bundle_path=bundle,
            dry_run=dry_run,
            confirm=confirm,
            progress=_progress,
        )
    except (UndoError, ConnectorError, ProgressError, BundleError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        console.print("  Nothing was removed.")
        raise typer.Exit(EXIT_ERROR) from exc
    except typer.Abort:
        console.print("\nCancelled: nothing was removed.")
        raise typer.Exit(EXIT_CANCELLED) from None
    if dry_run:
        _show_plan(result.plan)
        console.print("\n[bold]Dry run:[/bold] nothing was removed.")
        return
    if result.cancelled or result.report is None:
        console.print("Cancelled: nothing was removed.")
        raise typer.Exit(EXIT_CANCELLED)
    paths = write_undo_report(result.report, report_dir)
    _show_report(result.report, paths)
    raise typer.Exit(EXIT_OK if result.report.ok else EXIT_NOT_VERIFIED)
