"""Known domains and their canonical payload schemas.

The engine and the conformance kit use :func:`validate` to reject items whose payload is not
valid, canonical data for a domain this version of USMP knows. Items of unknown domains are
passed through unchecked, so connectors for new domains still work.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from usmp.core.models import Item, Selection
from usmp.domains import calendar
from usmp.domains.calendar import model as calendar_model
from usmp.domains.calendar import select as calendar_select


@dataclass(frozen=True)
class Domain:
    name: str
    schema_version: int
    types: Mapping[str, type[BaseModel]]
    problems: Callable[[str, Mapping[str, Any]], list[str]]
    select: Callable[[Iterable[Item], Selection], set[str]] | None = None
    """The ``usmp_id`` of the items a selection keeps; ``None`` if the domain cannot select."""


KNOWN: dict[tuple[str, int], Domain] = {
    (calendar.DOMAIN, calendar.SCHEMA_VERSION): Domain(
        name=calendar.DOMAIN,
        schema_version=calendar.SCHEMA_VERSION,
        types=calendar_model.ITEM_TYPES,
        problems=calendar.problems,
        select=calendar_select.select,
    ),
}


def get(name: str, schema_version: int) -> Domain | None:
    return KNOWN.get((name, schema_version))


def validate(item: Item) -> list[str]:
    """Problems with ``item``'s payload, or an empty list if it is valid (or the domain unknown)."""
    domain = get(item.domain, item.schema_version)
    return domain.problems(item.type, item.payload) if domain else []


def select(domain: str, items: Iterable[Item], selection: Selection) -> set[str]:
    """The ``usmp_id`` of the items of ``domain`` that ``selection`` keeps.

    Raises ``ValueError`` if the selection does not fit the data, or the domain cannot select.
    """
    selector = next((d.select for d in KNOWN.values() if d.name == domain and d.select), None)
    if selector is None:
        raise ValueError(f"USMP cannot choose part of {domain!r} data; migrate all of it")
    return selector(items, selection)
