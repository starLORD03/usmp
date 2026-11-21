"""Canonical JSON, content hashes and stable item identifiers.

Every implementation of USMP must produce byte-identical canonical JSON for the same value, so
that content hashes computed by different tools (or different languages) agree:

* object keys sorted by Unicode code point, no insignificant whitespace;
* strings emitted as UTF-8, not ``\\u``-escaped;
* ``NaN`` and ``Infinity`` are not allowed.

Floats are allowed but discouraged in payloads: number formatting differs between languages.
Domain schemas should use strings for decimal values.
"""

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

HASH_PREFIX = "sha256:"

# Namespace for deterministic usmp_id values. Never change it: ids would stop being stable.
USMP_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/starLORD03/usmp")


def canonical_json(value: Any) -> bytes:
    """Serialise ``value`` to canonical JSON bytes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return ``sha256:<hex digest>`` for ``data``."""
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def project(payload: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """Keep only the top-level ``fields`` of ``payload`` that are present."""
    wanted = set(fields)
    return {key: value for key, value in payload.items() if key in wanted}


def content_hash(
    *,
    domain: str,
    type: str,
    schema_version: int,
    payload: Mapping[str, Any],
) -> str:
    """Hash the provider-independent part of an item.

    The source reference and connector extensions are deliberately excluded: an item read back
    from the target has a different source but must hash the same if nothing was lost.
    """
    return sha256_hex(
        canonical_json(
            {
                "domain": domain,
                "type": type,
                "schema_version": schema_version,
                "payload": dict(payload),
            }
        )
    )


def usmp_id_for(connector: str, native_id: str) -> str:
    """Derive the stable ``usmp_id`` for an item from where it was first read."""
    return f"usmp:{uuid.uuid5(USMP_ID_NAMESPACE, f'{connector}:{native_id}')}"
