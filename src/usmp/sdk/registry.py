"""Discover connectors through the ``usmp.connectors`` entry-point group."""

import difflib
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points

from usmp.sdk.connector import Connector, ConnectorConfig, ConnectorError

ENTRY_POINT_GROUP = "usmp.connectors"


@dataclass(frozen=True)
class BrokenConnector:
    """An installed connector that failed to load."""

    name: str
    target: str
    error: str


def _load(ep: EntryPoint) -> type[Connector]:
    obj = ep.load()
    if not (isinstance(obj, type) and issubclass(obj, Connector)):
        raise TypeError(f"{ep.value} is not a Connector subclass")
    if obj.name != ep.name:
        raise TypeError(f"entry point {ep.name!r} loads a connector named {obj.name!r}")
    return obj


def discover() -> tuple[dict[str, type[Connector]], list[BrokenConnector]]:
    """Return ``(connectors by name, connectors that failed to load)``."""
    found: dict[str, type[Connector]] = {}
    broken: list[BrokenConnector] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            found[ep.name] = _load(ep)
        except Exception as exc:  # a broken plugin must not break the CLI
            broken.append(BrokenConnector(ep.name, ep.value, f"{type(exc).__name__}: {exc}"))
    return dict(sorted(found.items())), broken


def get(name: str) -> type[Connector]:
    connectors, broken = discover()
    if name in connectors:
        return connectors[name]
    for b in broken:
        if b.name == name:
            raise ConnectorError(f"connector {name!r} is installed but failed to load: {b.error}")
    close = difflib.get_close_matches(name, connectors, n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    available = ", ".join(connectors) or "none"
    raise ConnectorError(f"unknown connector {name!r}.{hint} Available: {available}")


def parse_spec(spec: str) -> tuple[str, dict[str, str]]:
    """Parse ``name`` or ``name:key=value,key=value`` into a name and a config mapping."""
    name, _, rest = spec.partition(":")
    name = name.strip()
    if not name:
        raise ConnectorError(f"invalid connector {spec!r}: expected NAME or NAME:key=value,...")
    config: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in rest.split(","))):
        key, sep, value = part.partition("=")
        if not sep or not key.strip():
            raise ConnectorError(f"invalid option {part!r} in {spec!r}: expected key=value")
        config[key.strip()] = value.strip()
    return name, config


def create(spec: str, extra: ConnectorConfig | None = None) -> Connector:
    """Instantiate a connector from a command-line spec such as ``jsondir:path=./out``."""
    name, config = parse_spec(spec)
    return get(name)({**config, **(extra or {})})
