"""Calendar domain (``usmp.calendar/v1``): models, canonical form and item builders."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from usmp.core.models import Item
from usmp.domains.calendar.model import (
    DOMAIN,
    ITEM_TYPES,
    SCHEMA_VERSION,
    Alarm,
    Attachment,
    Attendee,
    Calendar,
    Conference,
    Event,
    Person,
)


def canonicalize(type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``payload`` and return it in canonical form.

    Raises ``ValueError`` (a pydantic ``ValidationError``) if the payload is not valid.
    """
    model = ITEM_TYPES.get(type)
    if model is None:
        raise ValueError(
            f"unknown calendar item type {type!r}; expected one of {sorted(ITEM_TYPES)}"
        )
    return model.model_validate(dict(payload)).model_dump(mode="json", exclude_defaults=True)


def problems(type: str, payload: Mapping[str, Any]) -> list[str]:
    """Why ``payload`` is not a valid, canonical calendar item (empty list if it is)."""
    try:
        canonical = canonicalize(type, payload)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(p) for p in err['loc']) or type}: {err['msg']}" for err in exc.errors()
        ]
    except ValueError as exc:
        return [str(exc)]
    if canonical != dict(payload):
        changed = sorted(k for k in {*canonical, *payload} if canonical.get(k) != payload.get(k))
        return [f"not in canonical form (fields: {', '.join(changed)}); use canonicalize()"]
    return []


def _item(
    type: str,
    connector: str,
    native_id: str,
    payload: Mapping[str, Any],
    parent: str | None,
    extensions: Mapping[str, Mapping[str, Any]] | None,
) -> Item:
    return Item.create(
        connector=connector,
        native_id=native_id,
        domain=DOMAIN,
        type=type,
        schema_version=SCHEMA_VERSION,
        payload=canonicalize(type, payload),
        parent=parent,
        extensions=extensions,
    )


def calendar_item(
    connector: str,
    native_id: str,
    payload: Mapping[str, Any],
    *,
    extensions: Mapping[str, Mapping[str, Any]] | None = None,
) -> Item:
    """Build a canonical ``calendar`` item."""
    return _item("calendar", connector, native_id, payload, None, extensions)


def event_item(
    connector: str,
    native_id: str,
    payload: Mapping[str, Any],
    *,
    parent: str | None = None,
    extensions: Mapping[str, Mapping[str, Any]] | None = None,
) -> Item:
    """Build a canonical ``event`` item, usually with its calendar's ``usmp_id`` as ``parent``."""
    return _item("event", connector, native_id, payload, parent, extensions)


__all__ = [
    "DOMAIN",
    "SCHEMA_VERSION",
    "Alarm",
    "Attachment",
    "Attendee",
    "Calendar",
    "Conference",
    "Event",
    "Person",
    "calendar_item",
    "canonicalize",
    "event_item",
    "problems",
]
