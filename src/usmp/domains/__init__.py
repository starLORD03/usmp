"""Known domains and their canonical payload schemas.

The engine and the conformance kit use :func:`validate` to reject items whose payload is not
valid, canonical data for a domain this version of USMP knows. Items of unknown domains are
passed through unchecked, so connectors for new domains still work.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from usmp.core.models import Item
from usmp.domains import calendar
from usmp.domains.calendar import model as calendar_model


@dataclass(frozen=True)
class Domain:
    name: str
    schema_version: int
    types: Mapping[str, type[BaseModel]]
    problems: Callable[[str, Mapping[str, Any]], list[str]]


KNOWN: dict[tuple[str, int], Domain] = {
    (calendar.DOMAIN, calendar.SCHEMA_VERSION): Domain(
        name=calendar.DOMAIN,
        schema_version=calendar.SCHEMA_VERSION,
        types=calendar_model.ITEM_TYPES,
        problems=calendar.problems,
    ),
}


def get(name: str, schema_version: int) -> Domain | None:
    return KNOWN.get((name, schema_version))


def validate(item: Item) -> list[str]:
    """Problems with ``item``'s payload, or an empty list if it is valid (or the domain unknown)."""
    domain = get(item.domain, item.schema_version)
    return domain.problems(item.type, item.payload) if domain else []
