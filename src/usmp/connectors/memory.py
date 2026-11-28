"""An in-memory connector for tests and examples.

Capabilities can be passed programmatically, which lets tests model targets that cannot write or
read some fields. Every call is recorded in ``calls`` so tests can prove a source was never
written to.
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import ClassVar

from usmp.core.hashing import project
from usmp.core.models import (
    ANY,
    CapabilityManifest,
    ConnectorInfo,
    DomainCapability,
    Item,
    TypeCapability,
)
from usmp.sdk.connector import (
    Connector,
    ConnectorConfig,
    ConnectorError,
    ExportRecord,
    Reader,
    Writer,
    WriteResult,
)


def _restamp(item: Item, native_id: str, fields: set[str]) -> Item:
    """Copy ``item`` as stored by this connector, keeping only ``fields`` of the payload."""
    return Item.create(
        usmp_id=item.usmp_id,
        connector=MemoryConnector.name,
        native_id=native_id,
        domain=item.domain,
        type=item.type,
        schema_version=item.schema_version,
        parent=item.parent,
        payload=project(item.payload, fields),
        extensions=item.extensions,
    )


class MemoryConnector(Connector):
    name = "memory"
    display_name = "In-memory store"
    description = "Keeps items in memory for the lifetime of the process. For tests and demos."
    config_keys: ClassVar[Mapping[str, str]] = {"domain": "domain to declare (default: demo)"}

    def __init__(
        self,
        config: ConnectorConfig | None = None,
        *,
        capabilities: CapabilityManifest | None = None,
        items: Iterable[Item] = (),
    ) -> None:
        super().__init__(config)
        self._capabilities = capabilities
        self.items: dict[str, Item] = {i.source.native_id: i for i in items}
        self.calls: list[str] = []

    def capabilities(self) -> CapabilityManifest:
        if self._capabilities is not None:
            return self._capabilities
        return CapabilityManifest(
            connector=self.name,
            display_name=self.display_name,
            version="1",
            domains=[
                DomainCapability(
                    domain=self.config.get("domain", "demo"),
                    schema_version=1,
                    types=[TypeCapability(type=ANY, read_fields=[ANY], write_fields=[ANY])],
                )
            ],
        )

    def info(self) -> ConnectorInfo:
        return ConnectorInfo(
            connector=self.name, display_name=self.display_name, label=self.label or "memory"
        )

    def open_reader(self) -> Reader:
        self.calls.append("open_reader")
        return _MemoryReader(self)

    def open_writer(self) -> Writer:
        self.calls.append("open_writer")
        return _MemoryWriter(self)


class _MemoryReader(Reader):
    def __init__(self, connector: MemoryConnector) -> None:
        self._c = connector

    def read(self, domain: str) -> Iterator[ExportRecord]:
        self._c.calls.append(f"read:{domain}")
        caps = self._c.capabilities()
        for native_id, item in list(self._c.items.items()):
            cap = caps.type_capability(domain, item.type)
            if item.domain != domain or cap is None:
                continue
            yield ExportRecord(item=_restamp(item, native_id, cap.readable(item.payload)))


class _MemoryWriter(Writer):
    def __init__(self, connector: MemoryConnector) -> None:
        self._c = connector

    def already_written(
        self, items: Sequence[Item], known: Mapping[str, WriteResult]
    ) -> Iterator[WriteResult]:
        stored = {item.usmp_id: native_id for native_id, item in self._c.items.items()}
        for item in items:
            if item.usmp_id in stored:
                yield WriteResult(usmp_id=item.usmp_id, native_id=stored[item.usmp_id])

    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        caps = self._c.capabilities()
        for item in items:
            self._c.calls.append(f"write:{item.usmp_id}")
            cap = caps.type_capability(item.domain, item.type)
            if cap is None:
                raise ConnectorError(f"cannot write {item.domain}/{item.type} items")
            native_id = f"mem-{len(self._c.items) + 1}"
            self._c.items[native_id] = _restamp(item, native_id, cap.writable(item.payload))
            yield WriteResult(usmp_id=item.usmp_id, native_id=native_id)

    def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
        self._c.calls.append(f"read_back:{domain}")
        caps = self._c.capabilities()
        for native_id in native_ids:
            item = self._c.items.get(native_id)
            cap = caps.type_capability(domain, item.type) if item else None
            if item is not None and cap is not None:
                yield _restamp(item, native_id, cap.readable(item.payload))
