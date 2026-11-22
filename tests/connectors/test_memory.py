from tests.conftest import make_capabilities, make_event
from usmp.connectors.memory import MemoryConnector


def test_default_capabilities_accept_everything() -> None:
    caps = MemoryConnector({"domain": "demo"}).capabilities()
    cap = caps.type_capability("demo", "anything")

    assert cap is not None
    assert cap.readable({"a"}) == {"a"}


def test_reader_projects_to_readable_fields_and_filters_domain() -> None:
    item = make_event("1", title="T", start="S", notes="x")
    other = make_event("2").model_copy(update={"domain": "contacts"})  # skips validation
    c = MemoryConnector(
        capabilities=make_capabilities(read=["title", "start"]), items=[item, other]
    )

    with c.open_reader() as reader:
        records = list(reader.read("demo"))

    assert [r.item.payload for r in records] == [{"title": "T", "start": "S"}]
    assert records[0].item.usmp_id == item.usmp_id
    assert c.calls == ["open_reader", "read:demo"]


def test_writer_drops_unwritable_fields_and_reads_back() -> None:
    target = MemoryConnector(capabilities=make_capabilities(write=["title"], read=["title"]))
    item = make_event("1", title="T", start="S")

    with target.open_writer() as writer:
        results = list(writer.write([item]))
        back = list(writer.read_back("demo", [results[0].native_id, "unknown"]))

    assert results[0].usmp_id == item.usmp_id
    assert [b.payload for b in back] == [{"title": "T"}]
    assert back[0].source.connector == "memory"


def test_writer_rejects_unsupported_types() -> None:
    import pytest

    from usmp.sdk.connector import ConnectorError

    target = MemoryConnector(capabilities=make_capabilities())
    todo = make_event("1").model_copy(update={"type": "todo"})

    with pytest.raises(ConnectorError, match="cannot write demo/todo"):
        list(target.open_writer().write([todo]))


def test_info_has_default_label() -> None:
    assert MemoryConnector().info().label == "memory"
