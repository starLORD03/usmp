"""Checks every connector must pass.

Use them directly, or subclass :class:`ConnectorConformance` in your test suite::

    from usmp.testing import ConnectorConformance

    class TestMyConnector(ConnectorConformance):
        domain = "calendar"

        def make_connector(self):
            return MyConnector({"url": "..."})

        def sample_items(self):
            return [...]          # canonical items your connector should be able to write

        def provider_state(self):
            return ...            # optional: a snapshot proving reads change nothing
"""

import re
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from usmp import domains
from usmp.core.hashing import canonical_json
from usmp.core.models import CapabilityManifest, Item
from usmp.sdk.connector import Connector, ExportRecord, Reader, Writer

MUTATING_NAMES = frozenset(
    {"write", "delete", "update", "put", "post", "patch", "create", "remove", "save", "insert"}
)
_RAW_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class ConformanceError(AssertionError):
    """A connector broke the USMP connector contract."""


def _fail(connector: Connector, message: str) -> ConformanceError:
    return ConformanceError(f"{connector.name}: {message}")


def check_capabilities(connector: Connector) -> CapabilityManifest:
    """The capability manifest is valid and consistent with the connector."""
    manifest = CapabilityManifest.model_validate(connector.capabilities().model_dump())
    if manifest.connector != connector.name:
        raise _fail(connector, f"manifest names connector {manifest.connector!r}")
    if not manifest.domains:
        raise _fail(connector, "manifest declares no domains")
    return manifest


def check_reader_is_read_only(connector: Connector) -> None:
    """The object returned by ``open_reader`` offers no way to change the provider."""
    reader = connector.open_reader()
    if not isinstance(reader, Reader):
        raise _fail(connector, "open_reader() must return a usmp.sdk.Reader")
    with reader:
        if isinstance(reader, Writer):
            raise _fail(connector, "the reader is also a Writer; sources must be read-only")
        exposed = sorted(
            name
            for name in dir(reader)
            if not name.startswith("_")
            and any(name == m or name.startswith(f"{m}_") for m in MUTATING_NAMES)
        )
        if exposed:
            raise _fail(connector, f"the reader exposes mutating methods: {', '.join(exposed)}")


def check_read(
    connector: Connector,
    domain: str,
    provider_state: Callable[[], Any] | None = None,
) -> list[ExportRecord]:
    """Reading follows the contract and, if ``provider_state`` is given, changes nothing."""
    manifest = connector.capabilities()
    before = provider_state() if provider_state else None
    with connector.open_reader() as reader:
        records = list(reader.read(domain))
    if provider_state is not None and provider_state() != before:
        raise _fail(connector, "reading changed the provider's state")

    seen: set[str] = set()
    all_ids = {r.item.usmp_id for r in records}
    for record in records:
        item = record.item
        if item.domain != domain:
            raise _fail(connector, f"returned a {item.domain!r} item when asked for {domain!r}")
        if item.source.connector != connector.name:
            raise _fail(connector, f"item source names connector {item.source.connector!r}")
        if item.usmp_id in seen:
            raise _fail(connector, f"returned {item.usmp_id} twice")
        cap = manifest.type_capability(domain, item.type)
        if cap is None:
            raise _fail(connector, f"returned {item.type!r} items, which it does not declare")
        undeclared = set(item.payload) - cap.readable(item.payload)
        if undeclared:
            raise _fail(connector, f"returned undeclared fields {sorted(undeclared)}")
        issues = domains.validate(item)
        if issues:
            raise _fail(connector, f"returned an invalid {item.type!r} item: {issues[0]}")
        if item.parent in all_ids and item.parent not in seen:
            raise _fail(connector, f"{item.usmp_id} came before its parent {item.parent}")
        if record.raw_name is not None and not _RAW_NAME.fullmatch(record.raw_name):
            raise _fail(connector, f"invalid raw_name {record.raw_name!r}")
        for loss in record.losses:
            if loss.usmp_id != item.usmp_id or loss.stage != "export":
                raise _fail(connector, f"export loss {loss} does not belong to {item.usmp_id}")
        seen.add(item.usmp_id)
    return records


def check_round_trip(connector: Connector, items: Sequence[Item], domain: str) -> list[Item]:
    """Writing then reading back preserves every field the connector can both write and read."""
    manifest = connector.capabilities()
    with connector.open_writer() as writer:
        results = list(writer.write(items))
        by_id = {r.usmp_id: r for r in results}
        if len(results) != len(items) or set(by_id) != {i.usmp_id for i in items}:
            raise _fail(connector, "write() must yield exactly one result per item")
        native = [by_id[i.usmp_id].native_id for i in items]
        if len(set(native)) != len(native):
            raise _fail(connector, "write() returned duplicate native ids")
        back = {b.source.native_id: b for b in writer.read_back(domain, native)}

    unexpected = set(back) - set(native)
    if unexpected:
        raise _fail(connector, f"read_back() returned items it was not asked for: {unexpected}")
    for item, native_id in zip(items, native, strict=True):
        if native_id not in back:
            raise _fail(connector, f"{item.usmp_id} was written but could not be read back")
        cap = manifest.type_capability(domain, item.type)
        if cap is None:
            raise _fail(connector, f"wrote {item.type!r} items, which it does not declare")
        stored = back[native_id]
        issues = domains.validate(stored)
        if issues:
            raise _fail(connector, f"read_back() returned an invalid item: {issues[0]}")
        for name in sorted(cap.writable(item.payload) & cap.readable(item.payload)):
            if name not in stored.payload or canonical_json(stored.payload[name]) != canonical_json(
                item.payload[name]
            ):
                raise _fail(
                    connector,
                    f"field {name!r} of {item.usmp_id} changed: "
                    f"{item.payload[name]!r} -> {stored.payload.get(name)!r}",
                )
    return list(back.values())


def check_rerun(connector: Connector, items: Sequence[Item]) -> None:
    """After a write, a new writer finds every item through ``already_written`` (spec §8.1)."""
    with connector.open_writer() as writer:
        first = {r.usmp_id: r.native_id for r in writer.write(items)}
    with connector.open_writer() as writer:
        found = {r.usmp_id: r.native_id for r in writer.already_written(items, {})}
        missing = [i.usmp_id for i in items if i.usmp_id not in found]
        if missing:
            raise _fail(connector, f"already_written() did not find {missing[0]} after writing it")
        if found != first:
            raise _fail(connector, "already_written() returned different native ids than write()")
        if list(writer.write([])):
            raise _fail(connector, "write([]) must write nothing")


class ConnectorConformance:
    """Mix-in for pytest. Subclass it as ``Test<Name>`` and fill in the hooks."""

    domain: ClassVar[str]
    can_read: ClassVar[bool] = True
    can_write: ClassVar[bool] = True
    finds_written: ClassVar[bool] = False
    """Set when the writer recognises items it wrote before (``Writer.already_written``)."""

    def make_connector(self) -> Connector:
        """Return a fresh connector, pointed at a test account or a temporary location."""
        raise NotImplementedError

    def sample_items(self) -> list[Item]:
        """Canonical items the connector should be able to write (parents first)."""
        raise NotImplementedError

    def provider_state(self) -> Any:
        """Optional snapshot of the provider, compared before and after reading."""
        return None

    def test_capabilities(self) -> None:
        check_capabilities(self.make_connector())

    def test_reader_is_read_only(self) -> None:
        if self.can_read:
            check_reader_is_read_only(self.make_connector())

    def test_read_follows_contract(self) -> None:
        if self.can_read:
            connector = self.make_connector()
            if self.can_write:
                check_round_trip(connector, self.sample_items(), self.domain)
            check_read(connector, self.domain, self.provider_state)

    def test_write_read_back_round_trip(self) -> None:
        if self.can_write:
            check_round_trip(self.make_connector(), self.sample_items(), self.domain)

    def test_rerun_finds_written_items(self) -> None:
        if self.can_write and self.finds_written:
            check_rerun(self.make_connector(), self.sample_items())
