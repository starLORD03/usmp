import pytest

from usmp.core.models import (
    CapabilityManifest,
    ConnectorInfo,
    DomainCapability,
    Item,
    TypeCapability,
)

EVENT_FIELDS = ["title", "start", "end", "location", "color"]


def make_event(native_id: str, connector: str = "memory", **payload: object) -> Item:
    return Item.create(
        connector=connector,
        native_id=native_id,
        domain="calendar",
        type="event",
        schema_version=1,
        payload=payload or {"title": f"Event {native_id}", "start": "2026-01-05T09:00:00Z"},
    )


def make_capabilities(
    connector: str = "memory",
    read: list[str] | None = None,
    write: list[str] | None = None,
) -> CapabilityManifest:
    return CapabilityManifest(
        connector=connector,
        display_name=connector.title(),
        version="1",
        domains=[
            DomainCapability(
                domain="calendar",
                schema_version=1,
                types=[
                    TypeCapability(
                        type="event",
                        read_fields=EVENT_FIELDS if read is None else read,
                        write_fields=EVENT_FIELDS if write is None else write,
                    )
                ],
            )
        ],
    )


@pytest.fixture
def capabilities() -> CapabilityManifest:
    return make_capabilities()


@pytest.fixture
def source_info() -> ConnectorInfo:
    return ConnectorInfo(connector="memory", display_name="Memory", label="test calendar")
