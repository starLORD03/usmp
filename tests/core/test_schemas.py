import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from usmp.core import schemas

COMMITTED = Path(__file__).resolve().parents[2] / "spec" / "schemas"


@pytest.mark.parametrize("relative", sorted(schemas.generate()))
def test_committed_schema_matches_models(relative: str) -> None:
    generated = schemas.render(schemas.generate()[relative])
    committed = (COMMITTED / relative).read_text(encoding="utf-8")

    assert committed == generated, (
        f"spec/schemas/{relative} is out of date. "
        "Run `uv run python -m usmp.core.schemas` and commit the result."
    )


@pytest.mark.parametrize("schema", schemas.generate().values(), ids=lambda s: s["$id"])
def test_schemas_are_valid_draft_2020_12(schema: dict[str, object]) -> None:
    Draft202012Validator.check_schema(schema)


def test_no_unexpected_committed_files() -> None:
    committed = {p.relative_to(COMMITTED).as_posix() for p in COMMITTED.rglob("*.json")}

    assert committed == set(schemas.generate())


def test_item_example_validates_against_published_schema() -> None:
    from usmp.core.models import Item

    item = Item.create(
        connector="memory",
        native_id="1",
        domain="demo",
        type="event",
        schema_version=1,
        payload={"title": "Standup"},
    )
    schema = json.loads((COMMITTED / "core" / "item.schema.json").read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(item.model_dump(mode="json"))


def test_write_creates_files(tmp_path: Path) -> None:
    written = schemas.write(tmp_path)

    assert {p.relative_to(tmp_path).as_posix() for p in written} == set(schemas.generate())
