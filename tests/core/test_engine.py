from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

import pytest

from tests.conftest import make_capabilities, make_event
from usmp.connectors.memory import MemoryConnector
from usmp.core.bundle import BundleReader
from usmp.core.engine import ALL_FIELDS, MigrationError, Preview, choose_domain, migrate
from usmp.core.models import (
    CapabilityManifest,
    DomainCapability,
    Item,
    ItemStatus,
    LossReason,
    LossRecord,
    TypeCapability,
)
from usmp.sdk.connector import ConnectorError, ExportRecord, Reader, Writer, WriteResult

FIELDS = {"title": "Standup", "start": "2026-01-05T09:00:00Z", "location": "Room 1"}


def source_with(*items: Item, **caps: list[str]) -> MemoryConnector:
    return MemoryConnector(capabilities=make_capabilities("memory", **caps), items=items)


def run(source: MemoryConnector, target: MemoryConnector, tmp_path: Path, **kw: object):  # type: ignore[no-untyped-def]
    return migrate(source, target, bundle_path=tmp_path / "b.usmp", **kw)  # type: ignore[arg-type]


def test_clean_migration_is_verified(tmp_path: Path) -> None:
    items = [make_event(str(n), **FIELDS) for n in range(3)]
    source, target = source_with(*items), MemoryConnector(capabilities=make_capabilities())

    result = run(source, target, tmp_path)

    assert result.report is not None
    assert result.report.ok
    assert result.report.summary.verified == 3
    assert result.report.domain == "demo"
    assert all(r.compared_fields == ["location", "start", "title"] for r in result.report.items)
    assert len(target.items) == 3


def test_source_is_only_read(tmp_path: Path) -> None:
    source = source_with(make_event("1", **FIELDS))
    before = dict(source.items)

    run(source, MemoryConnector(capabilities=make_capabilities()), tmp_path)

    assert source.calls == ["open_reader", "read:demo"]
    assert source.items == before


def test_bundle_snapshot_written_and_valid(tmp_path: Path) -> None:
    source = source_with(make_event("1", **FIELDS), make_event("2", **FIELDS))

    result = run(source, MemoryConnector(capabilities=make_capabilities()), tmp_path)

    with BundleReader(result.preview.bundle_path) as bundle:
        assert bundle.validate() == []
        assert len(list(bundle.iter_items())) == 2


def test_unwritable_field_is_declared_and_verified_with_loss(tmp_path: Path) -> None:
    source = source_with(make_event("1", **FIELDS))
    target = MemoryConnector(capabilities=make_capabilities(write=["title", "start"]))

    result = run(source, target, tmp_path)

    assert result.report is not None and result.report.ok
    [item] = result.report.items
    assert item.status is ItemStatus.VERIFIED_WITH_DECLARED_LOSS
    assert item.declared_loss_fields == ["location"]
    assert item.compared_fields == ["start", "title"]
    [loss] = result.preview.losses
    assert (loss.field, loss.reason, loss.stage) == ("location", "unsupported_by_target", "plan")


def test_write_only_field_is_not_compared(tmp_path: Path) -> None:
    source = source_with(make_event("1", **FIELDS))
    target = MemoryConnector(capabilities=make_capabilities(read=["title", "start"]))

    result = run(source, target, tmp_path)

    assert result.report is not None and result.report.ok
    [loss] = result.preview.losses
    assert loss.reason is LossReason.NOT_READABLE_BY_TARGET
    assert result.report.items[0].compared_fields == ["start", "title"]


def test_unsupported_type_is_blocked_and_missing(tmp_path: Path) -> None:
    todo = Item.create(
        connector="memory",
        native_id="t1",
        domain="demo",
        type="todo",
        schema_version=1,
        payload={"title": "Buy milk"},
    )
    source = MemoryConnector(
        capabilities=CapabilityManifest(
            connector="memory",
            display_name="Memory",
            version="1",
            domains=[
                DomainCapability(
                    domain="demo",
                    schema_version=1,
                    types=[
                        TypeCapability(type="event", read_fields=["title"]),
                        TypeCapability(type="todo", read_fields=["title"]),
                    ],
                )
            ],
        ),
        items=[make_event("1", title="Meet"), todo],
    )
    target = MemoryConnector(capabilities=make_capabilities())

    result = run(source, target, tmp_path)

    assert result.preview.blocked_items == 1
    assert result.report is not None and not result.report.ok
    statuses = {r.type: r.status for r in result.report.items}
    assert statuses == {"event": ItemStatus.VERIFIED, "todo": ItemStatus.MISSING}
    assert any(loss.field == ALL_FIELDS for loss in result.report.losses)
    assert len(target.items) == 1


class CorruptingWriter(Writer):
    """Writes items but changes their titles, and loses one."""

    def __init__(self) -> None:
        self.stored: dict[str, Item] = {}

    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        for n, item in enumerate(items):
            native = f"x{n}"
            payload = {**item.payload, "title": "WRONG"} if n == 0 else dict(item.payload)
            if n != 2:
                self.stored[native] = Item.create(
                    connector="bad",
                    native_id=native,
                    domain=item.domain,
                    type=item.type,
                    schema_version=1,
                    payload=payload,
                )
            yield WriteResult(usmp_id=item.usmp_id, native_id=native)

    def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
        yield from (self.stored[n] for n in native_ids if n in self.stored)


class FakeTarget(MemoryConnector):
    def __init__(self, writer: Writer) -> None:
        super().__init__(capabilities=make_capabilities("memory"))
        self.writer = writer

    def open_writer(self) -> Writer:
        return self.writer


def test_corruption_and_loss_on_target_are_detected(tmp_path: Path) -> None:
    source = source_with(*(make_event(str(n), **FIELDS) for n in range(3)))

    result = run(source, FakeTarget(CorruptingWriter()), tmp_path)

    assert result.report is not None and not result.report.ok
    by_status = {r.status: r for r in result.report.items}
    assert by_status[ItemStatus.MISMATCH].mismatched_fields == ["title"]
    assert ItemStatus.MISSING in by_status
    assert result.report.summary.verified == 1


class FailingWriter(Writer):
    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        for n, item in enumerate(items):
            if n == 1:
                raise ConnectorError("rate limited")
            yield WriteResult(usmp_id=item.usmp_id, native_id=f"n{n}")

    def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
        raise ConnectorError("server gone")


def test_write_failure_is_recorded_not_raised(tmp_path: Path) -> None:
    source = source_with(make_event("1"), make_event("2"))

    result = run(source, FakeTarget(FailingWriter()), tmp_path)

    assert result.report is not None and not result.report.ok
    assert result.report.errors == [
        "writing stopped after 1 items: rate limited",
        "reading back failed: server gone",
    ]
    assert result.report.summary.missing == 2


def test_target_that_cannot_open_is_recorded(tmp_path: Path) -> None:
    class Unreachable(MemoryConnector):
        def open_writer(self) -> Writer:
            raise ConnectorError("sign-in failed")

    result = run(
        source_with(make_event("1")), Unreachable(capabilities=make_capabilities()), tmp_path
    )

    assert result.report is not None
    assert result.report.errors == ["cannot open In-memory store for writing: sign-in failed"]


def test_export_losses_and_raw_are_kept(tmp_path: Path) -> None:
    item = make_event("1", title="T")

    class LossySource(MemoryConnector):
        def open_reader(self) -> Reader:
            class R(Reader):
                def read(self, domain: str) -> Iterator[ExportRecord]:
                    loss = LossRecord(
                        usmp_id=item.usmp_id,
                        field="x-google-color",
                        reason=LossReason.UNSUPPORTED_BY_CANONICAL,
                        stage="export",
                    )
                    yield ExportRecord(item=item, losses=[loss], raw=b"{}", raw_name="1.json")

            return R()

    result = run(
        LossySource(capabilities=make_capabilities()),
        MemoryConnector(capabilities=make_capabilities()),
        tmp_path,
    )

    assert result.report is not None and result.report.ok
    assert result.report.items[0].status is ItemStatus.VERIFIED_WITH_DECLARED_LOSS
    with BundleReader(result.preview.bundle_path) as bundle:
        assert bundle.raw_names == ["1.json"]
        assert len(bundle.losses()) == 1


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    target = MemoryConnector(capabilities=make_capabilities())

    result = run(source_with(make_event("1")), target, tmp_path, dry_run=True)

    assert result.report is None
    assert result.preview.total == 1
    assert target.calls == []
    assert result.preview.bundle_path.exists()


def test_declined_confirmation_writes_nothing(tmp_path: Path) -> None:
    target = MemoryConnector(capabilities=make_capabilities())
    seen: list[Preview] = []

    def decline(preview: Preview) -> bool:
        seen.append(preview)
        return False

    result = run(source_with(make_event("1")), target, tmp_path, confirm=decline)

    assert result.cancelled
    assert target.calls == []
    assert seen[0].counts == {"event": 1}


class TestPlanChecks:
    def test_source_cannot_read(self, tmp_path: Path) -> None:
        with pytest.raises(MigrationError, match="cannot read 'demo'"):
            run(
                source_with(read=[]),
                MemoryConnector(capabilities=make_capabilities()),
                tmp_path,
                domain="demo",
            )

    def test_target_cannot_write(self, tmp_path: Path) -> None:
        with pytest.raises(MigrationError, match="cannot write 'demo'"):
            run(source_with(), MemoryConnector(capabilities=make_capabilities(write=[])), tmp_path)

    def test_schema_version_mismatch(self, tmp_path: Path) -> None:
        v2 = CapabilityManifest(
            connector="memory",
            display_name="M",
            version="1",
            domains=[
                DomainCapability(
                    domain="demo",
                    schema_version=2,
                    types=[
                        TypeCapability(type="event", read_fields=["title"], write_fields=["title"])
                    ],
                )
            ],
        )
        with pytest.raises(MigrationError, match="schema version mismatch"):
            run(source_with(), MemoryConnector(capabilities=v2), tmp_path)

    def test_source_returning_undeclared_fields_is_rejected(self, tmp_path: Path) -> None:
        class Liar(MemoryConnector):
            def open_reader(self) -> Reader:
                class R(Reader):
                    def read(self, domain: str) -> Iterator[ExportRecord]:
                        yield ExportRecord(item=make_event("1", title="T", surprise="x"))

                return R()

        with pytest.raises(MigrationError, match="undeclared fields"):
            run(
                Liar(capabilities=make_capabilities()),
                MemoryConnector(capabilities=make_capabilities()),
                tmp_path,
            )

    def test_source_returning_wrong_domain_is_rejected(self, tmp_path: Path) -> None:
        class Wrong(MemoryConnector):
            def open_reader(self) -> Reader:
                class R(Reader):
                    def read(self, domain: str) -> Iterator[ExportRecord]:
                        yield ExportRecord(
                            item=Item.create(
                                connector="m",
                                native_id="1",
                                domain="contacts",
                                type="event",
                                schema_version=1,
                                payload={},
                            )
                        )

                return R()

        with pytest.raises(MigrationError, match="returned a 'contacts' item"):
            run(
                Wrong(capabilities=make_capabilities()),
                MemoryConnector(capabilities=make_capabilities()),
                tmp_path,
            )


def test_choose_domain() -> None:
    two = CapabilityManifest(
        connector="m",
        display_name="Multi",
        version="1",
        domains=[
            DomainCapability(
                domain=d, schema_version=1, types=[TypeCapability(type="x", read_fields=["a"])]
            )
            for d in ("demo", "contacts")
        ],
    )
    assert choose_domain(make_capabilities(), None) == "demo"
    assert choose_domain(two, "contacts") == "contacts"
    with pytest.raises(MigrationError, match="several domains"):
        choose_domain(two, None)
