"""Generate the published JSON Schemas from the models.

Run ``python -m usmp.core.schemas`` from the repository root to regenerate ``spec/schemas/``.
A test fails if the committed files drift from the models.
"""

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from usmp.core.models import (
    BundleManifest,
    CapabilityManifest,
    Item,
    LossRecord,
    UndoReport,
    VerificationReport,
)

SCHEMA_BASE_URL = "https://starlord03.github.io/usmp/schemas/"
DEFAULT_OUTPUT = Path("spec/schemas")

CORE_MODELS: dict[str, type[BaseModel]] = {
    "item": Item,
    "loss-record": LossRecord,
    "capability-manifest": CapabilityManifest,
    "bundle-manifest": BundleManifest,
    "verification-report": VerificationReport,
    "undo-report": UndoReport,
}


def published_models() -> dict[str, type[BaseModel]]:
    """``{path relative to spec/schemas without extension: model}``."""
    from usmp import domains  # imported lazily: domains depend on core

    models = {f"core/{name}": model for name, model in CORE_MODELS.items()}
    for domain in domains.KNOWN.values():
        for type_name, model in domain.types.items():
            models[f"domains/{domain.name}/v{domain.schema_version}/{type_name}"] = model
    return models


def generate() -> dict[str, dict[str, Any]]:
    """Return ``{relative file path: schema}`` for every published model."""
    schemas = {}
    for path, model in published_models().items():
        schema = model.model_json_schema(mode="serialization")
        schemas[f"{path}.schema.json"] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SCHEMA_BASE_URL}{path}.schema.json",
            **schema,
        }
    return schemas


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def write(output: Path = DEFAULT_OUTPUT) -> list[Path]:
    written = []
    for relative, schema in generate().items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(schema), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT):
        print(path)
