import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from usmp.core import schemas

COMMITTED = Path(__file__).resolve().parents[2] / "spec" / "schemas" / "core"


@pytest.mark.parametrize("filename", sorted(schemas.generate()))
def test_committed_schema_matches_models(filename: str) -> None:
    generated = schemas.render(schemas.generate()[filename])
    committed = (COMMITTED / filename).read_text(encoding="utf-8")

    assert committed == generated, (
        f"spec/schemas/core/{filename} is out of date. "
        "Run `uv run python -m usmp.core.schemas` and commit the result."
    )


@pytest.mark.parametrize("schema", schemas.generate().values(), ids=lambda s: s["$id"])
def test_schemas_are_valid_draft_2020_12(schema: dict[str, object]) -> None:
    Draft202012Validator.check_schema(schema)


def test_no_unexpected_committed_files() -> None:
    assert {p.name for p in COMMITTED.glob("*.json")} == set(schemas.generate())


def test_item_example_validates_against_published_schema() -> None:
    from usmp.core.models import Item

    item = Item.create(
        connector="memory",
        native_id="1",
        domain="calendar",
        type="event",
        schema_version=1,
        payload={"title": "Standup"},
    )
    schema = json.loads((COMMITTED / "item.schema.json").read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(item.model_dump(mode="json"))


def test_write_creates_files(tmp_path: Path) -> None:
    written = schemas.write(tmp_path)

    assert {p.name for p in written} == set(schemas.generate())
