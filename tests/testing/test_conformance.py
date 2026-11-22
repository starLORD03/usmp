import hashlib
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from tests.conftest import make_capabilities, make_event
from usmp.connectors.jsondir import JsonDirConnector
from usmp.connectors.memory import MemoryConnector
from usmp.core.models import CapabilityManifest, Item, LossReason, LossRecord
from usmp.sdk.connector import ExportRecord, Reader, Writer, WriteResult
from usmp.testing import (
    ConformanceError,
    ConnectorConformance,
    check_capabilities,
    check_read,
    check_reader_is_read_only,
    check_round_trip,
)


def calendar_items() -> list[Item]:
    cal = Item.create(
        connector="seed",
        native_id="cal",
        domain="calendar",
        type="calendar",
        schema_version=1,
        payload={"name": "Work"},
    )
    event = Item.create(
        connector="seed",
        native_id="e1",
        domain="calendar",
        type="event",
        schema_version=1,
        parent=cal.usmp_id,
        payload={"title": "Standup", "start": "2026-01-05T09:00:00Z"},
    )
    return [cal, event]


# --- Built-in connectors pass the kit ----------------------------------------------------------


class TestMemoryConformance(ConnectorConformance):
    domain = "calendar"

    def make_connector(self) -> MemoryConnector:
        self.connector = MemoryConnector({"domain": "calendar"})
        return self.connector

    def sample_items(self) -> list[Item]:
        return calendar_items()

    def provider_state(self) -> Any:
        return dict(self.connector.items)


class TestJsonDirConformance(ConnectorConformance):
    domain = "calendar"

    @pytest.fixture(autouse=True)
    def _dir(self, tmp_path: Path) -> None:
        self.path = tmp_path

    def make_connector(self) -> JsonDirConnector:
        return JsonDirConnector({"path": str(self.path), "domain": "calendar"})

    def sample_items(self) -> list[Item]:
        return calendar_items()

    def provider_state(self) -> Any:
        return {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.path.rglob("*.json")
        }


def test_hooks_must_be_implemented() -> None:
    class Incomplete(ConnectorConformance):
        domain = "calendar"

    with pytest.raises(NotImplementedError):
        Incomplete().make_connector()
    with pytest.raises(NotImplementedError):
        Incomplete().sample_items()
    assert Incomplete().provider_state() is None


def test_read_only_and_write_only_flags_skip_checks() -> None:
    class ReadOnly(TestMemoryConformance):
        can_write = False

    class WriteOnly(TestMemoryConformance):
        can_read = False

    ReadOnly().test_read_follows_contract()
    ReadOnly().test_write_read_back_round_trip()
    WriteOnly().test_reader_is_read_only()
    WriteOnly().test_read_follows_contract()


# --- Each check catches a broken connector -----------------------------------------------------


def broken(
    reader: Reader | None = None,
    writer: Writer | None = None,
    caps: CapabilityManifest | None = None,
) -> MemoryConnector:
    class Broken(MemoryConnector):
        def capabilities(self) -> CapabilityManifest:
            return caps or make_capabilities()

        def open_reader(self) -> Reader:
            return reader or super().open_reader()

        def open_writer(self) -> Writer:
            return writer or super().open_writer()

    return Broken()


def reader_yielding(*records: ExportRecord) -> Reader:
    class R(Reader):
        def read(self, domain: str) -> Iterator[ExportRecord]:
            yield from records

    return R()


def mem(item: Item, **changes: Any) -> Item:
    data = {
        "connector": "memory",
        "native_id": item.source.native_id,
        "domain": item.domain,
        "type": item.type,
        "schema_version": 1,
        "payload": item.payload,
        "parent": item.parent,
        **changes,
    }
    return Item.create(**data)


class TestCapabilityCheck:
    def test_wrong_connector_name(self) -> None:
        with pytest.raises(ConformanceError, match="manifest names connector 'other'"):
            check_capabilities(broken(caps=make_capabilities("other")))

    def test_no_domains(self) -> None:
        empty = CapabilityManifest(connector="memory", display_name="M", version="1", domains=[])
        with pytest.raises(ConformanceError, match="no domains"):
            check_capabilities(broken(caps=empty))


class TestReadOnlyCheck:
    def test_reader_with_delete_method(self) -> None:
        class Sneaky(Reader):
            def read(self, domain: str) -> Iterator[ExportRecord]:
                yield from ()

            def delete_event(self, native_id: str) -> None: ...

        with pytest.raises(ConformanceError, match="mutating methods: delete_event"):
            check_reader_is_read_only(broken(reader=Sneaky()))

    def test_reader_that_is_also_a_writer(self) -> None:
        class Both(Reader, Writer):
            def read(self, domain: str) -> Iterator[ExportRecord]:
                yield from ()

            def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
                yield from ()

            def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
                yield from ()

        with pytest.raises(ConformanceError, match="also a Writer"):
            check_reader_is_read_only(broken(reader=Both()))

    def test_not_a_reader(self) -> None:
        with pytest.raises(ConformanceError, match=r"must return a usmp\.sdk\.Reader"):
            check_reader_is_read_only(broken(reader=object()))  # type: ignore[arg-type]


class TestReadCheck:
    event = mem(make_event("1"))

    @pytest.mark.parametrize(
        ("record", "message"),
        [
            (ExportRecord(item=mem(make_event("1"), domain="contacts")), "'contacts' item"),
            (ExportRecord(item=make_event("1", connector="other")), "source names connector"),
            (ExportRecord(item=mem(make_event("1"), type="todo")), "does not declare"),
            (
                ExportRecord(item=mem(make_event("1"), payload={"secretive": 1})),
                "undeclared fields",
            ),
            (
                ExportRecord(item=mem(make_event("1")), raw=b"x", raw_name="../x"),
                "invalid raw_name",
            ),
            (
                ExportRecord(
                    item=mem(make_event("1")),
                    losses=[
                        LossRecord(
                            usmp_id=make_event("9").usmp_id,
                            field="f",
                            reason=LossReason.VALUE_CONVERTED,
                            stage="export",
                        )
                    ],
                ),
                "does not belong",
            ),
        ],
    )
    def test_violations(self, record: ExportRecord, message: str) -> None:
        with pytest.raises(ConformanceError, match=message):
            check_read(broken(reader=reader_yielding(record)), "calendar")

    def test_duplicates(self) -> None:
        record = ExportRecord(item=self.event)
        with pytest.raises(ConformanceError, match="twice"):
            check_read(broken(reader=reader_yielding(record, record)), "calendar")

    def test_child_before_parent(self) -> None:
        parent = mem(make_event("p"))
        child = mem(make_event("c"), parent=parent.usmp_id)
        reader = reader_yielding(ExportRecord(item=child), ExportRecord(item=parent))
        with pytest.raises(ConformanceError, match="before its parent"):
            check_read(broken(reader=reader), "calendar")

    def test_reading_changes_state(self) -> None:
        counter = iter(range(10))
        with pytest.raises(ConformanceError, match="changed the provider"):
            check_read(broken(reader=reader_yielding()), "calendar", lambda: next(counter))


class TestRoundTripCheck:
    items: ClassVar[list[Item]] = [make_event("1", title="T", start="S")]

    def writer(self, *, results: Any = None, back: Any = None) -> Writer:
        outer = self

        class W(Writer):
            def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
                yield from (
                    results
                    if results is not None
                    else [
                        WriteResult(usmp_id=i.usmp_id, native_id=f"n{n}")
                        for n, i in enumerate(items)
                    ]
                )

            def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
                yield from (back if back is not None else [mem(outer.items[0], native_id="n0")])

        return W()

    def test_passes_for_faithful_writer(self) -> None:
        assert len(check_round_trip(broken(writer=self.writer()), self.items, "calendar")) == 1

    def test_missing_results(self) -> None:
        with pytest.raises(ConformanceError, match="one result per item"):
            check_round_trip(broken(writer=self.writer(results=[])), self.items, "calendar")

    def test_duplicate_native_ids(self) -> None:
        two = [make_event("1"), make_event("2")]
        dup = [WriteResult(usmp_id=i.usmp_id, native_id="same") for i in two]
        with pytest.raises(ConformanceError, match="duplicate native ids"):
            check_round_trip(broken(writer=self.writer(results=dup)), two, "calendar")

    def test_unreadable_item(self) -> None:
        with pytest.raises(ConformanceError, match="could not be read back"):
            check_round_trip(broken(writer=self.writer(back=[])), self.items, "calendar")

    def test_unrequested_item(self) -> None:
        extra = [mem(self.items[0], native_id="n0"), mem(self.items[0], native_id="zzz")]
        with pytest.raises(ConformanceError, match="not asked for"):
            check_round_trip(broken(writer=self.writer(back=extra)), self.items, "calendar")

    def test_changed_field(self) -> None:
        changed = [mem(self.items[0], native_id="n0", payload={"title": "X", "start": "S"})]
        with pytest.raises(ConformanceError, match=r"field 'title' .* changed"):
            check_round_trip(broken(writer=self.writer(back=changed)), self.items, "calendar")

    def test_undeclared_type(self) -> None:
        todo = [mem(make_event("1"), type="todo")]
        back = [mem(todo[0], native_id="n0")]
        with pytest.raises(ConformanceError, match="does not declare"):
            check_round_trip(broken(writer=self.writer(back=back)), todo, "calendar")
