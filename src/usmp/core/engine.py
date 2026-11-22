"""The migration engine (spec §8).

``migrate()`` runs one domain from a source connector to a target connector:

    plan → export → snapshot (bundle) → preview → confirm → import → read back → verify → report

The source is only ever opened as a :class:`~usmp.sdk.connector.Reader`. Nothing is written to the
target unless ``confirm`` returns true, and a dry run stops after the preview.
"""

import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from usmp.core.bundle import BundleWriter
from usmp.core.hashing import canonical_json
from usmp.core.models import (
    BundleManifest,
    CapabilityManifest,
    ConnectorInfo,
    Item,
    ItemResult,
    ItemStatus,
    LossReason,
    LossRecord,
    ReportSummary,
    Severity,
    VerificationReport,
)
from usmp.sdk.connector import Connector, ConnectorError

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
            bundle.add_item(item)
            for loss in record.losses:
                bundle.add_loss(loss)
            if record.raw is not None and record.raw_name is not None:
                bundle.add_raw(record.raw_name, record.raw)
            out.items.append(item)
            out.losses.extend(record.losses)
    return out


def _differs(a: object, b: object) -> bool:
    return canonical_json(a) != canonical_json(b)


def _verify(
    item: Item,
    back: Item | None,
    target: CapabilityManifest,
    lost_fields: set[str],
    native_id: str | None,
) -> ItemResult:
    def result(status: ItemStatus, **fields: list[str]) -> ItemResult:
        return ItemResult(
            usmp_id=item.usmp_id,
            type=item.type,
            status=status,
            target_native_id=native_id,
            declared_loss_fields=sorted(lost_fields),
            **fields,
        )

    cap = target.type_capability(item.domain, item.type)
    if back is None or cap is None:
        return result(ItemStatus.MISSING)
    comparable = cap.writable(item.payload) & cap.readable(item.payload)
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


def migrate(
    source: Connector,
    target: Connector,
    *,
    bundle_path: Path,
    domain: str | None = None,
    dry_run: bool = False,
    confirm: Callable[[Preview], bool] = lambda _preview: True,
    progress: Callable[[str], None] = lambda _message: None,
) -> MigrationResult:
    """Migrate one domain from ``source`` to ``target``.

    Raises :class:`MigrationError` if the migration cannot start (incompatible connectors, a
    misbehaving source). Problems during import are recorded in the report instead.
    """
    started = datetime.now(UTC)
    source_caps, target_caps = source.capabilities(), target.capabilities()
    domain = choose_domain(source_caps, domain)
    check_plan(source_caps, target_caps, domain)

    progress(f"Exporting {domain} data from {source.display_name}…")
    with BundleWriter(bundle_path, source=source.info(), capabilities=source_caps) as bundle:
        exported = _export(source, domain, bundle)
    if bundle.manifest is None:  # pragma: no cover - close() always sets it
        raise MigrationError("the backup bundle was not written")

    preview = Preview(
        domain=domain,
        source=source.info(),
        target=target.info(),
        counts=dict(Counter(i.type for i in exported.items)),
        losses=[*exported.losses, *predict_losses(exported.items, target_caps, domain)],
        bundle=bundle.manifest,
        bundle_path=bundle_path,
    )
    if dry_run:
        return MigrationResult(preview=preview)
    if not confirm(preview):
        return MigrationResult(preview=preview, cancelled=True)

    blocked = {loss.usmp_id for loss in preview.losses if loss.field == ALL_FIELDS}
    to_write = [i for i in exported.items if i.usmp_id not in blocked]
    native_ids: dict[str, str] = {}
    import_losses: list[LossRecord] = []
    errors: list[str] = []

    progress(f"Writing {len(to_write)} items to {target.display_name}…")
    back_by_native: dict[str, Item] = {}
    try:
        with target.open_writer() as writer:
            try:
                for written in writer.write(to_write):
                    native_ids[written.usmp_id] = written.native_id
                    import_losses.extend(written.losses)
            except ConnectorError as exc:
                errors.append(f"writing stopped after {len(native_ids)} items: {exc}")
            progress("Reading items back to verify…")
            try:
                for read in writer.read_back(domain, list(native_ids.values())):
                    back_by_native[read.source.native_id] = read
            except ConnectorError as exc:
                errors.append(f"reading back failed: {exc}")
    except ConnectorError as exc:
        errors.append(f"cannot open {target.display_name} for writing: {exc}")

    all_losses = [*preview.losses, *import_losses]
    lost_by_item: dict[str, set[str]] = defaultdict(set)
    for loss in all_losses:
        lost_by_item[loss.usmp_id].add(loss.field)

    results = []
    for item in exported.items:
        native_id = native_ids.get(item.usmp_id)
        back = back_by_native.get(native_id) if native_id else None
        results.append(
            _verify(item, back, target_caps, lost_by_item.get(item.usmp_id, set()), native_id)
        )

    report = VerificationReport(
        run_id=uuid.uuid4().hex,
        started_at=started,
        finished_at=datetime.now(UTC),
        domain=domain,
        dry_run=False,
        source=preview.source,
        target=preview.target,
        summary=ReportSummary.from_results(results),
        items=results,
        losses=all_losses,
        errors=errors,
    )
    return MigrationResult(preview=preview, report=report)
