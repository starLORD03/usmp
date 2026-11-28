"""The migration engine (spec §8).

``migrate()`` runs one domain from a source connector to a target connector:

    plan → export → snapshot (bundle) → preview → confirm → import → read back → verify → report

The source is only ever opened as a :class:`~usmp.sdk.connector.Reader`. Nothing is written to the
target unless ``confirm`` returns true, and a dry run stops after the preview.
"""

import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from usmp import domains
from usmp.core.bundle import BundleError, BundleReader, BundleWriter
from usmp.core.hashing import canonical_json
from usmp.core.models import (
    BundleManifest,
    CapabilityManifest,
    ConnectorInfo,
    DataWarning,
    Item,
    ItemResult,
    ItemStatus,
    LossReason,
    LossRecord,
    ReportSummary,
    Selection,
    Severity,
    VerificationReport,
)
from usmp.core.progress import (
    ProgressError,
    ProgressHeader,
    ProgressLog,
    read_progress,
    safe_config,
)
from usmp.sdk.connector import Connector, ConnectorError, WriteResult

ALL_FIELDS = "*"
"""``LossRecord.field`` value meaning the whole item cannot be migrated."""


class MigrationError(Exception):
    """The migration cannot start or cannot continue."""


@dataclass(frozen=True)
class Preview:
    """What will happen, shown to the user before anything is written to the target."""

    domain: str
    source: ConnectorInfo
    target: ConnectorInfo
    counts: dict[str, int]
    """Items exported, by type."""
    losses: list[LossRecord]
    """Export losses plus losses predicted from the target's capabilities."""
    bundle: BundleManifest
    bundle_path: Path
    selection: Selection | None = None
    """The part of the exported data being migrated; ``None`` means all of it."""
    left_out: int = 0
    """Exported items outside the selection: in the backup, but not migrated."""
    warnings: list[DataWarning] = field(default_factory=list)
    """Odd things in the source data, e.g. events ending before they start (migrated as is)."""
    encrypted: bool = False
    """Whether the backup bundle is encrypted with a passphrase."""

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def blocked_items(self) -> int:
        """Items the target cannot store at all."""
        return len({loss.usmp_id for loss in self.losses if loss.field == ALL_FIELDS})


@dataclass
class MigrationResult:
    preview: Preview
    report: VerificationReport | None = None
    """``None`` for dry runs and cancelled migrations."""
    cancelled: bool = False


@dataclass
class _Exported:
    items: list[Item] = field(default_factory=list)
    losses: list[LossRecord] = field(default_factory=list)


def choose_domain(source: CapabilityManifest, requested: str | None) -> str:
    """Use the requested domain, or the source's only domain."""
    if requested:
        return requested
    readable = [d.domain for d in source.domains if source.can_read(d.domain)]
    if len(readable) == 1:
        return readable[0]
    raise MigrationError(
        f"{source.display_name} has several domains ({', '.join(readable)}); choose one"
    )


def check_plan(source: CapabilityManifest, target: CapabilityManifest, domain: str) -> None:
    """Fail early if the two connectors cannot migrate ``domain``."""
    if not source.can_read(domain):
        raise MigrationError(f"{source.display_name} cannot read {domain!r} data")
    if not target.can_write(domain):
        raise MigrationError(f"{target.display_name} cannot write {domain!r} data")
    src, dst = source.domain(domain), target.domain(domain)
    if src is not None and dst is not None and src.schema_version != dst.schema_version:
        raise MigrationError(
            f"schema version mismatch for {domain!r}: source v{src.schema_version}, "
            f"target v{dst.schema_version}"
        )


def predict_losses(items: list[Item], target: CapabilityManifest, domain: str) -> list[LossRecord]:
    """Losses that follow from the target's capabilities (spec §6, stage ``plan``)."""
    losses = []
    for item in items:
        cap = target.type_capability(domain, item.type)
        if cap is None:
            losses.append(
                LossRecord(
                    usmp_id=item.usmp_id,
                    field=ALL_FIELDS,
                    reason=LossReason.UNSUPPORTED_BY_TARGET,
                    severity=Severity.ERROR,
                    stage="plan",
                    detail=f"{target.display_name} cannot store {item.type!r} items",
                )
            )
            continue
        writable = cap.writable(item.payload)
        readable = cap.readable(item.payload)
        for name in sorted(item.payload):
            if name not in writable:
                losses.append(
                    LossRecord(
                        usmp_id=item.usmp_id,
                        field=name,
                        reason=LossReason.UNSUPPORTED_BY_TARGET,
                        stage="plan",
                    )
                )
            elif name not in readable:
                losses.append(
                    LossRecord(
                        usmp_id=item.usmp_id,
                        field=name,
                        reason=LossReason.NOT_READABLE_BY_TARGET,
                        severity=Severity.INFO,
                        stage="plan",
                        detail="written but cannot be read back to verify",
                    )
                )
    return losses


def _export(source: Connector, domain: str, bundle: BundleWriter) -> _Exported:
    caps = source.capabilities()
    out = _Exported()
    with source.open_reader() as reader:
        for record in reader.read(domain):
            item = record.item
            if item.domain != domain:
                raise MigrationError(
                    f"{source.display_name} returned a {item.domain!r} item "
                    f"when asked for {domain!r}"
                )
            cap = caps.type_capability(domain, item.type)
            undeclared = set(item.payload) - (cap.readable(item.payload) if cap else set())
            if undeclared:
                raise MigrationError(
                    f"{source.display_name} returned undeclared fields {sorted(undeclared)} on "
                    f"{item.type!r} items; its capability manifest is wrong"
                )
            issues = domains.validate(item)
            if issues:
                raise MigrationError(
                    f"{source.display_name} returned an invalid {item.domain} {item.type!r} item "
                    f"({item.source.native_id}): {'; '.join(issues[:3])}"
                )
            bundle.add_item(item)
            for loss in record.losses:
                bundle.add_loss(loss)
            if record.raw is not None and record.raw_name is not None:
                bundle.add_raw(record.raw_name, record.raw)
            out.items.append(item)
            out.losses.extend(record.losses)
    return out


def _choose(
    domain: str, items: list[Item], losses: list[LossRecord], selection: Selection | None
) -> tuple[list[Item], list[LossRecord], int]:
    """The items and losses ``selection`` keeps, and how many items it leaves out."""
    if selection is None:
        return items, losses, 0
    try:
        kept = domains.select(domain, items, selection)
    except ValueError as exc:
        raise MigrationError(str(exc)) from exc
    chosen = [i for i in items if i.usmp_id in kept]
    if not chosen:
        raise MigrationError(f"nothing matches the selection ({selection.describe()})")
    return chosen, [loss for loss in losses if loss.usmp_id in kept], len(items) - len(chosen)


def _differs(a: object, b: object) -> bool:
    return canonical_json(a) != canonical_json(b)


def _verify(
    item: Item,
    back: Item | None,
    target: CapabilityManifest,
    lost_fields: set[str],
    native_id: str | None,
    changed_by_target: frozenset[str] = frozenset(),
    *,
    already_present: bool = False,
) -> ItemResult:
    def result(status: ItemStatus, **fields: list[str]) -> ItemResult:
        return ItemResult.model_validate(
            {
                "usmp_id": item.usmp_id,
                "type": item.type,
                "status": status,
                "target_native_id": native_id,
                "declared_loss_fields": sorted(lost_fields),
                "already_present": already_present,
                **fields,
            }
        )

    cap = target.type_capability(item.domain, item.type)
    if back is None or cap is None:
        return result(ItemStatus.MISSING)
    comparable = cap.writable(item.payload) & cap.readable(item.payload) - changed_by_target
    compared = sorted(comparable)
    if item.projected_hash(comparable) == back.projected_hash(comparable):
        status = ItemStatus.VERIFIED_WITH_DECLARED_LOSS if lost_fields else ItemStatus.VERIFIED
        return result(status, compared_fields=compared)
    mismatched = [
        name
        for name in compared
        if name not in back.payload or _differs(item.payload[name], back.payload[name])
    ]
    return result(ItemStatus.MISMATCH, compared_fields=compared, mismatched_fields=mismatched)


def _write_and_verify(
    preview: Preview,
    items: list[Item],
    target: Connector,
    target_caps: CapabilityManifest,
    log: ProgressLog,
    known: Mapping[str, WriteResult],
    progress: Callable[[str], None],
    started: datetime,
    advance: Callable[[int, int], None],
) -> VerificationReport:
    """Import whatever is not on the target yet, read everything back and verify it."""
    domain = preview.domain
    blocked = {loss.usmp_id for loss in preview.losses if loss.field == ALL_FIELDS}
    to_write = [i for i in items if i.usmp_id not in blocked]
    results: dict[str, WriteResult] = {}
    present: set[str] = set()
    errors: list[str] = []

    back_by_native: dict[str, Item] = {}
    try:
        with target.open_writer() as writer:
            progress(f"Checking what is already in {target.display_name}…")
            try:
                for found in writer.already_written(to_write, known):
                    results[found.usmp_id] = found
                    present.add(found.usmp_id)
                    if found.usmp_id not in known:
                        log.record(found)
            except ConnectorError as exc:
                # Without knowing what is there, writing could only fail or duplicate.
                errors.append(f"cannot check what is already in the target: {exc}")
            else:
                remaining = [i for i in to_write if i.usmp_id not in present]
                skipped = f" ({len(present)} already there)" if present else ""
                progress(f"Writing {len(remaining)} items to {target.display_name}{skipped}…")
                written = 0
                advance(0, len(remaining))
                try:
                    for result in writer.write(remaining):
                        results[result.usmp_id] = result
                        log.record(result)
                        written += 1
                        advance(written, len(remaining))
                except ConnectorError as exc:
                    errors.append(f"writing stopped after {written} items: {exc}")
            progress("Reading items back to verify…")
            try:
                native_ids = [r.native_id for r in results.values()]
                for read in writer.read_back(domain, native_ids):
                    back_by_native[read.source.native_id] = read
            except ConnectorError as exc:
                errors.append(f"reading back failed: {exc}")
    except ConnectorError as exc:
        errors.append(f"cannot open {target.display_name} for writing: {exc}")

    import_losses = [loss for r in results.values() for loss in r.losses]
    all_losses = [*preview.losses, *import_losses]
    lost_by_item: dict[str, set[str]] = defaultdict(set)
    for loss in all_losses:
        lost_by_item[loss.usmp_id].add(loss.field)

    # Fields the target itself reported as changed on import are declared losses, not mismatches.
    changed_by_target: dict[str, set[str]] = defaultdict(set)
    for loss in import_losses:
        changed_by_target[loss.usmp_id].add(loss.field)

    verified = []
    for item in items:
        stored = results.get(item.usmp_id)
        native_id = stored.native_id if stored else None
        back = back_by_native.get(native_id) if native_id else None
        outcome = _verify(
            item,
            back,
            target_caps,
            lost_by_item.get(item.usmp_id, set()),
            native_id,
            frozenset(changed_by_target.get(item.usmp_id, set())),
            already_present=item.usmp_id in present,
        )
        verified.append(outcome)

    return VerificationReport(
        run_id=uuid.uuid4().hex,
        started_at=started,
        finished_at=datetime.now(UTC),
        domain=domain,
        dry_run=False,
        source=preview.source,
        target=preview.target,
        summary=ReportSummary.from_results(verified, left_out=preview.left_out),
        items=verified,
        losses=all_losses,
        errors=errors,
        selection=preview.selection,
        warnings=preview.warnings,
        backup_encrypted=preview.encrypted,
    )


def migrate(
    source: Connector,
    target: Connector,
    *,
    bundle_path: Path,
    domain: str | None = None,
    dry_run: bool = False,
    confirm: Callable[[Preview], bool] = lambda _preview: True,
    progress: Callable[[str], None] = lambda _message: None,
    selection: Selection | None = None,
    advance: Callable[[int, int], None] = lambda _done, _total: None,
    passphrase: str | None = None,
) -> MigrationResult:
    """Migrate one domain from ``source`` to ``target``.

    Everything the source returns is saved in the backup bundle; with a ``selection`` only the
    part it keeps is written and verified (spec §8.2). ``progress`` receives a message at each
    step and ``advance`` the number of items written so far out of those to write. With a
    ``passphrase`` the backup is encrypted (spec §7.1) and the progress record kept private.

    Raises :class:`MigrationError` if the migration cannot start (incompatible connectors, a
    misbehaving source). Problems during import are recorded in the report instead.
    """
    started = datetime.now(UTC)
    source_caps, target_caps = source.capabilities(), target.capabilities()
    domain = choose_domain(source_caps, domain)
    check_plan(source_caps, target_caps, domain)

    progress(f"Exporting {domain} data from {source.display_name}…")
    try:
        writer = BundleWriter(
            bundle_path, source=source.info(), capabilities=source_caps, passphrase=passphrase
        )
    except ValueError as exc:
        raise MigrationError(str(exc)) from exc
    with writer as bundle:
        exported = _export(source, domain, bundle)
    if bundle.manifest is None:  # pragma: no cover - close() always sets it
        raise MigrationError("the backup bundle was not written")

    items, losses, left_out = _choose(domain, exported.items, exported.losses, selection)
    preview = Preview(
        domain=domain,
        source=source.info(),
        target=target.info(),
        counts=dict(Counter(i.type for i in items)),
        losses=[*losses, *predict_losses(items, target_caps, domain)],
        bundle=bundle.manifest,
        bundle_path=bundle_path,
        selection=selection,
        left_out=left_out,
        warnings=domains.warnings(domain, items),
        encrypted=passphrase is not None,
    )
    if dry_run:
        return MigrationResult(preview=preview)
    if not confirm(preview):
        return MigrationResult(preview=preview, cancelled=True)

    log = ProgressLog.start(
        bundle_path,
        items_sha256=preview.bundle.items_sha256,
        domain=domain,
        target=preview.target,
        target_connector=target.name,
        target_config=target.config,
        selection=selection,
        private=passphrase is not None,
    )
    with log:
        report = _write_and_verify(
            preview, items, target, target_caps, log, {}, progress, started, advance
        )
    return MigrationResult(preview=preview, report=report)


def resume(
    target: Connector,
    *,
    bundle_path: Path,
    dry_run: bool = False,
    confirm: Callable[[Preview], bool] = lambda _preview: True,
    progress: Callable[[str], None] = lambda _message: None,
    selection: Selection | None = None,
    advance: Callable[[int, int], None] = lambda _done, _total: None,
    passphrase: str | None = None,
) -> MigrationResult:
    """Finish a migration from its backup bundle, without reading the source again.

    An encrypted backup needs its ``passphrase``; the progress record then stays private.

    Items the progress record (or the target itself) shows as already written are not written
    again; everything is still read back and verified. Without a progress record this imports the
    bundle (or the part ``selection`` keeps) into ``target``. With a record, the run keeps the
    selection it started with; a different ``selection`` is refused.
    """
    started = datetime.now(UTC)
    try:
        record = read_progress(bundle_path)
        with BundleReader(bundle_path, passphrase=passphrase) as bundle:
            encrypted = bundle.encrypted
            problems = bundle.validate()
            if problems:
                raise MigrationError(f"the backup {bundle_path} is damaged: {problems[0]}")
            manifest = bundle.manifest
            items = list(bundle.iter_items())
            export_losses = bundle.losses()
    except (BundleError, ProgressError) as exc:
        raise MigrationError(str(exc)) from exc
    domains_in_bundle = sorted({c.domain for c in manifest.counts})
    if record is not None:
        header, known = record
        if header.items_sha256 != manifest.items_sha256:
            raise MigrationError(
                f"the progress record next to {bundle_path} belongs to a different backup"
            )
        if header.target_connector != target.name:
            raise MigrationError(
                f"this migration was going to {header.target.display_name}, "
                f"not {target.display_name}"
            )
        domain = header.domain
        if selection is not None and selection != header.selection:
            started_with = header.selection.describe() if header.selection else "everything"
            raise MigrationError(
                f"this migration was started with a different selection ({started_with}); "
                "resume it without choosing again"
            )
        selection = header.selection
    else:
        known = {}
        domain = choose_domain(
            manifest.capabilities, domains_in_bundle[0] if len(domains_in_bundle) == 1 else None
        )
    target_caps = target.capabilities()
    check_plan(manifest.capabilities, target_caps, domain)
    items = [i for i in items if i.domain == domain]
    items, export_losses, left_out = _choose(domain, items, export_losses, selection)
    preview = Preview(
        domain=domain,
        source=manifest.source,
        target=target.info(),
        counts=dict(Counter(i.type for i in items)),
        losses=[*export_losses, *predict_losses(items, target_caps, domain)],
        bundle=manifest,
        bundle_path=bundle_path,
        selection=selection,
        left_out=left_out,
        warnings=domains.warnings(domain, items),
        encrypted=encrypted,
    )
    if dry_run:
        return MigrationResult(preview=preview)
    if not confirm(preview):
        return MigrationResult(preview=preview, cancelled=True)
    header = (
        record[0].model_copy(update={"target": preview.target})
        if record is not None
        else ProgressHeader(
            started_at=started,
            items_sha256=manifest.items_sha256,
            domain=domain,
            target=preview.target,
            target_connector=target.name,
            target_config=safe_config(target.config),
            selection=selection,
        )
    )
    with ProgressLog(bundle_path, header, known, private=encrypted) as log:
        report = _write_and_verify(
            preview, items, target, target_caps, log, known, progress, started, advance
        )
    return MigrationResult(preview=preview, report=report)
