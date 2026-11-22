from pathlib import Path

import pytest

from tests.conftest import make_event
from usmp.connectors.jsondir import JsonDirConnector
from usmp.core.models import Item
from usmp.sdk.connector import ConnectorError


def connector(path: Path) -> JsonDirConnector:
    return JsonDirConnector({"path": str(path), "domain": "demo"})


def calendar_and_events() -> list[Item]:
    cal = Item.create(
        connector="src",
        native_id="cal",
        domain="demo",
        type="demo",
        schema_version=1,
        payload={"name": "Work"},
    )
    events = [
        Item.create(
            connector="src",
            native_id=f"e{n}",
            domain="demo",
            type="event",
            schema_version=1,
            parent=cal.usmp_id,
            payload={"title": f"E{n}"},
        )
        for n in range(3)
    ]
    return [*events, cal]


def test_write_then_read_round_trip_preserves_ids_and_payloads(tmp_path: Path) -> None:
    items = calendar_and_events()
    with connector(tmp_path).open_writer() as w:
        results = list(w.write(items))
        back = list(w.read_back("demo", [r.native_id for r in results]))

    assert {r.usmp_id for r in results} == {i.usmp_id for i in items}
    assert {b.usmp_id: b.content_hash for b in back} == {i.usmp_id: i.content_hash for i in items}
    assert sorted(p.parent.name for p in tmp_path.glob("*/*.json")) == ["demo"] + ["event"] * 3

    with connector(tmp_path).open_reader() as r:
        read = [rec.item for rec in r.read("demo")]
    assert read[0].type == "demo", "parents must come before children"
    assert all(i.source.connector == "jsondir" for i in read)


def test_refuses_to_overwrite(tmp_path: Path) -> None:
    item = make_event("1")
    list(connector(tmp_path).open_writer().write([item]))

    with pytest.raises(ConnectorError, match="refusing to overwrite"):
        list(connector(tmp_path).open_writer().write([item]))


def test_reader_needs_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(ConnectorError, match="is not a directory"):
        connector(tmp_path / "missing").open_reader()


def test_required_options() -> None:
    with pytest.raises(ConnectorError, match="missing required option 'domain'"):
        JsonDirConnector({"path": "x"}).capabilities()


def test_corrupt_item_reported(tmp_path: Path) -> None:
    item = make_event("1")
    [result] = connector(tmp_path).open_writer().write([item])
    path = tmp_path / f"{result.native_id}.json"
    path.write_text(path.read_text().replace("Event 1", "Edited"))

    with pytest.raises(ConnectorError, match="cannot read item"):
        list(connector(tmp_path).open_reader().read("demo"))


def test_ignores_foreign_files_traversal_ids_and_other_domains(tmp_path: Path) -> None:
    list(connector(tmp_path).open_writer().write([make_event("1")]))
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "event" / "not-a-uuid.json").write_text("{}")

    reader_items = list(connector(tmp_path).open_reader().read("demo"))
    other_domain = list(connector(tmp_path).open_reader().read("contacts"))
    back = list(connector(tmp_path).open_writer().read_back("demo", ["../../etc/passwd"]))

    assert len(reader_items) == 1
    assert other_domain == []
    assert back == []


def test_read_back_skips_missing_and_other_domains(tmp_path: Path) -> None:
    [result] = connector(tmp_path).open_writer().write([make_event("1")])
    w = connector(tmp_path).open_writer()

    assert list(w.read_back("contacts", [result.native_id])) == []
    assert list(w.read_back("demo", ["event/00000000-0000-0000-0000-000000000000"])) == []
