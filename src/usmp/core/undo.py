"""Undoing a migration (spec §8.3): remove only what it created, only if nobody changed it since.

``undo()`` works from a migration's backup bundle and its progress record:

    read record → check target → read back → compare → preview → confirm → remove → report

An item is removed only when all of these hold:

* the progress record says this migration wrote it (items that were already on the target are
  never touched, nor items whose origin the record does not state);
* it still exists and still matches what was verified after the migration (by content hash), or,
  for a migration that never got as far as verifying, what was migrated;
* the target's writer confirms it still carries USMP's marker for it, and, for containers such as
  calendars, that the migration created it and it is empty (see :meth:`Writer.remove`).

Everything else is kept and listed in the report with the reason.
"""

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from usmp.core.bundle import BundleError, BundleReader
from usmp.core.models import (
    ConnectorInfo,
    Item,
    UndoItemResult,
    UndoOutcome,
    UndoReport,
    UndoSummary,
)
from usmp.core.progress import (
    ProgressError,
    ProgressHeader,
    WrittenItem,
    read_progress_items,
    safe_config,
)
from usmp.sdk.connector import Connector, ConnectorError, Removal, RemovalOutcome

# Options that say how to connect or what to move, rather than where: they do not identify the
# place a migration wrote to, and may differ between the migration and its undo.
_NOT_LOCATION = frozenset({"label", "workers", "domain"})

CHANGED = "changed on the target after the migration"
NOT_MIGRATED = "does not match what was migrated (it may have been changed)"
ORIGIN_UNKNOWN = "the record does not say whether this migration created it"
NOT_REPORTED = "not removed: the target did not report on it"


class UndoError(Exception):
    """The undo cannot start: no record, wrong target, or a target that cannot remove items."""


@dataclass(frozen=True)
class UndoPlan:
    """What undo will do, shown to the user before anything is removed."""

    domain: str
    target: ConnectorInfo
    migration_source: ConnectorInfo
    bundle_path: Path
    bundle_items_sha256: str
    removals: list[Removal]
    """Items to remove, children before their containers."""
    kept: list[UndoItemResult] = field(default_factory=list)
    """Items this migration wrote that will be left in place, with the reason."""
    gone: list[UndoItemResult] = field(default_factory=list)
    """Items this migration wrote that are no longer on the target."""
    left_alone: int = 0
    """Items that were on the target before the migration: never touched."""


@dataclass
class UndoResult:
    plan: UndoPlan
    report: UndoReport | None = None
    """``None`` for dry runs and cancelled undos."""
    cancelled: bool = False


def _where(config: dict[str, str]) -> dict[str, str]:
    return {
        k: v for k, v in safe_config(config).items()
        if k not in _NOT_LOCATION and not k.endswith("_env")
    }  # fmt: skip


def _check_target(header: ProgressHeader, target: Connector) -> None:
    if target.name != header.target_connector:
        raise UndoError(
            f"this migration went to {header.target.display_name}, not {target.display_name}"
        )
    if _where(target.config) != _where(header.target_config):
        raise UndoError(
            f"this migration went to a different {target.display_name} location; undo only "
            "removes items from the place the migration wrote to"
        )
    source = _where(header.source_config or {})
    if header.source_connector == target.name and source and source == _where(target.config):
        raise UndoError("this is where the migration read from: undo never touches a source")


def _matches(
    current: Item, written: WrittenItem, migrated: Item | None, target: Connector, domain: str
) -> bool:
    """Whether ``current`` is still what the migration left on the target."""
    if written.verified_hash is not None:
        return current.content_hash == written.verified_hash
    if migrated is None:
        return False
    # Never verified (the run stopped first): compare with what was migrated, as verify would.
    cap = target.capabilities().type_capability(domain, migrated.type)
    if cap is None:
        return False
    changed = {loss.field for loss in written.losses}
    fields = cap.writable(migrated.payload) & cap.readable(migrated.payload) - changed
    return migrated.projected_hash(fields) == current.projected_hash(fields)


def _ordered(removals: list[Removal], by_id: dict[str, Item]) -> list[Removal]:
    """Children before their containers: the deepest items first."""

    def depth(usmp_id: str) -> int:
        d, parent = 0, by_id[usmp_id].parent if usmp_id in by_id else None
        while parent in by_id and d <= len(by_id):
            d, parent = d + 1, by_id[parent].parent
        return d

    return sorted(removals, key=lambda r: (-depth(r.usmp_id), r.usmp_id))


@dataclass(frozen=True)
class _Migration:
    header: ProgressHeader
    written: dict[str, WrittenItem]
    items: list[Item]
    items_sha256: str
    source: ConnectorInfo


def _load(bundle_path: Path) -> _Migration:
    try:
        record = read_progress_items(bundle_path)
        with BundleReader(bundle_path) as bundle:
            problems = bundle.validate()
            if problems:
                raise UndoError(f"the backup {bundle_path} is damaged: {problems[0]}")
            items = list(bundle.iter_items())
            manifest = bundle.manifest
    except (BundleError, ProgressError) as exc:
        raise UndoError(str(exc)) from exc
    if record is None:
        raise UndoError(
            f"there is no record of what this migration wrote (next to {bundle_path}), so it "
            "cannot be undone safely"
        )
    header, written = record
    if header.items_sha256 != manifest.items_sha256:
        raise UndoError(f"the progress record next to {bundle_path} belongs to a different backup")
    return _Migration(header, written, items, manifest.items_sha256, manifest.source)


def undo(
    target: Connector,
    *,
    bundle_path: Path,
    dry_run: bool = False,
    confirm: Callable[[UndoPlan], bool] = lambda _plan: True,
    progress: Callable[[str], None] = lambda _message: None,
) -> UndoResult:
    """Remove from ``target`` what the migration saved in ``bundle_path`` created there.

    Raises :class:`UndoError` if the undo cannot start. Problems while removing are recorded in
    the report instead.
    """
    started = datetime.now(UTC)
    migration = _load(bundle_path)
    header, written, items = migration.header, migration.written, migration.items
    items_sha256 = migration.items_sha256
    _check_target(header, target)
    domain = header.domain
    by_id = {item.usmp_id: item for item in items}

    ours = [w for w in written.values() if w.already_present is False]
    unknown = [w for w in written.values() if w.already_present is None]
    left_alone = sum(1 for w in written.values() if w.already_present)

    def result(w: WrittenItem, outcome: UndoOutcome, reason: str | None = None) -> UndoItemResult:
        item = by_id.get(w.usmp_id)
        return UndoItemResult(
            usmp_id=w.usmp_id,
            type=item.type if item else "unknown",
            target_native_id=w.native_id,
            outcome=outcome,
            reason=reason,
        )

    kept = [result(w, UndoOutcome.KEPT, ORIGIN_UNKNOWN) for w in unknown]
    gone: list[UndoItemResult] = []
    removals: list[Removal] = []
    errors: list[str] = []
    with target.open_writer() as writer:
        if not writer.can_remove:
            raise UndoError(
                f"{target.display_name} cannot remove items, so this migration cannot be undone"
            )
        progress(f"Reading what the migration wrote back from {target.display_name}…")
        try:
            current = {
                i.source.native_id: i for i in writer.read_back(domain, [w.native_id for w in ours])
            }
        except ConnectorError as exc:
            raise UndoError(f"cannot read the target: {exc}") from exc
        for w in ours:
            now = current.get(w.native_id)
            if now is None:
                gone.append(result(w, UndoOutcome.ALREADY_GONE))
            elif not _matches(now, w, by_id.get(w.usmp_id), target, domain):
                reason = CHANGED if w.verified_hash is not None else NOT_MIGRATED
                kept.append(result(w, UndoOutcome.KEPT, reason))
            else:
                item = by_id.get(w.usmp_id)
                removals.append(
                    Removal(w.usmp_id, w.native_id, item.type if item else now.type, w.created)
                )
        plan = UndoPlan(
            domain=domain,
            target=target.info(),
            migration_source=migration.source,
            bundle_path=bundle_path,
            bundle_items_sha256=items_sha256,
            removals=_ordered(removals, by_id),
            kept=kept,
            gone=gone,
            left_alone=left_alone,
        )
        if dry_run:
            return UndoResult(plan)
        if not confirm(plan):
            return UndoResult(plan, cancelled=True)

        progress(f"Removing {len(plan.removals)} items from {target.display_name}…")
        outcomes: dict[str, UndoItemResult] = {}
        pending = {r.usmp_id: written[r.usmp_id] for r in plan.removals}
        try:
            for removed in writer.remove(domain, plan.removals):
                asked = pending.get(removed.usmp_id)
                if asked is not None:
                    outcomes[removed.usmp_id] = result(
                        asked, UndoOutcome(removed.outcome.value), removed.reason
                    )
        except ConnectorError as exc:
            errors.append(f"removing stopped after {len(outcomes)} items: {exc}")
    finished = _report_items(plan, outcomes, pending.values(), result)
    report = UndoReport(
        run_id=uuid.uuid4().hex,
        started_at=started,
        finished_at=datetime.now(UTC),
        domain=domain,
        target=plan.target,
        migration_source=plan.migration_source,
        bundle_items_sha256=items_sha256,
        summary=UndoSummary.from_results(finished, left_alone=left_alone),
        items=finished,
        errors=errors,
    )
    return UndoResult(plan, report)


def _report_items(
    plan: UndoPlan,
    outcomes: dict[str, UndoItemResult],
    asked: Iterable[WrittenItem],
    result: Callable[[WrittenItem, UndoOutcome, str | None], UndoItemResult],
) -> list[UndoItemResult]:
    done = [outcomes.get(w.usmp_id) or result(w, UndoOutcome.KEPT, NOT_REPORTED) for w in asked]
    return sorted([*done, *plan.kept, *plan.gone], key=lambda r: r.usmp_id)


__all__ = ["RemovalOutcome", "UndoError", "UndoPlan", "UndoResult", "undo"]
