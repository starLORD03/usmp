"""Generate the published JSON Schemas from the core models.

Run ``python -m usmp.core.schemas`` from the repository root to regenerate
``spec/schemas/core/``. A test fails if the committed files drift from the models.
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
    VerificationReport,
)

SCHEMA_BASE_URL = "https://starlord03.github.io/usmp/schemas/core/"
DEFAULT_OUTPUT = Path("spec/schemas/core")

CORE_MODELS: dict[str, type[BaseModel]] = {
    "item": Item,
    "loss-record": LossRecord,
    "capability-manifest": CapabilityManifest,
    "bundle-manifest": BundleManifest,
    "verification-report": VerificationReport,
}


def generate() -> dict[str, dict[str, Any]]:
    """Return ``{file name: schema}`` for every core model."""
    schemas = {}
    for name, model in CORE_MODELS.items():
        schema = model.model_json_schema(mode="serialization")
        schemas[f"{name}.schema.json"] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{SCHEMA_BASE_URL}{name}.schema.json",
            **schema,
        }
    return schemas


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def write(output: Path = DEFAULT_OUTPUT) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, schema in generate().items():
        path = output / filename
        path.write_text(render(schema), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT):
        print(path)
