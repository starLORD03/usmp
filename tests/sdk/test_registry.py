from importlib.metadata import EntryPoint

import pytest

from usmp.connectors.jsondir import JsonDirConnector
from usmp.connectors.memory import MemoryConnector
from usmp.sdk import registry
from usmp.sdk.connector import ConnectorError


def fake_entry_points(*eps: EntryPoint):  # type: ignore[no-untyped-def]
    def _entry_points(group: str) -> tuple[EntryPoint, ...]:
        assert group == registry.ENTRY_POINT_GROUP
        return eps

    return _entry_points


def ep(name: str, value: str) -> EntryPoint:
    return EntryPoint(name=name, value=value, group=registry.ENTRY_POINT_GROUP)


def test_builtin_connectors_are_discovered() -> None:
    found, broken = registry.discover()

    assert found["memory"] is MemoryConnector
    assert found["jsondir"] is JsonDirConnector
    assert broken == []


def test_broken_plugins_are_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry,
        "entry_points",
        fake_entry_points(
            ep("memory", "usmp.connectors.memory:MemoryConnector"),
            ep("missing", "no_such_module:Thing"),
            ep("notaconnector", "pathlib:Path"),
            ep("misnamed", "usmp.connectors.memory:MemoryConnector"),
        ),
    )

    found, broken = registry.discover()

    assert list(found) == ["memory"]
    errors = {b.name: b.error for b in broken}
    assert "ModuleNotFoundError" in errors["missing"]
    assert "not a Connector subclass" in errors["notaconnector"]
    assert "connector named 'memory'" in errors["misnamed"]
    with pytest.raises(ConnectorError, match="installed but failed to load"):
        registry.get("missing")


def test_unknown_connector_suggests_close_match() -> None:
    with pytest.raises(ConnectorError, match="Did you mean 'jsondir'"):
        registry.get("jsondr")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("memory", ("memory", {})),
        ("jsondir:path=./out", ("jsondir", {"path": "./out"})),
        (
            " jsondir : path = a , domain=calendar ,",
            ("jsondir", {"path": "a", "domain": "calendar"}),
        ),
        ("x:key=a=b", ("x", {"key": "a=b"})),
    ],
)
def test_parse_spec(spec: str, expected: tuple[str, dict[str, str]]) -> None:
    assert registry.parse_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", ":path=x", "jsondir:path", "jsondir:=x"])
def test_parse_spec_errors(spec: str) -> None:
    with pytest.raises(ConnectorError, match="invalid"):
        registry.parse_spec(spec)


def test_create_merges_extra_config() -> None:
    c = registry.create("jsondir:path=a", {"domain": "calendar"})

    assert isinstance(c, JsonDirConnector)
    assert c.config == {"path": "a", "domain": "calendar"}
