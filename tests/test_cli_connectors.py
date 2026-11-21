from importlib.metadata import EntryPoint

import pytest
from typer.testing import CliRunner

from usmp.cli.app import app
from usmp.sdk import registry

runner = CliRunner()


def test_list_shows_builtins_and_roles() -> None:
    result = runner.invoke(app, ["connectors", "list"])

    assert result.exit_code == 0, result.output
    assert "jsondir" in result.output
    assert "source, target" in result.output


def test_list_reports_broken_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = EntryPoint(name="bad", value="no_such_module:X", group=registry.ENTRY_POINT_GROUP)
    monkeypatch.setattr(registry, "entry_points", lambda group: (broken,))

    result = runner.invoke(app, ["connectors", "list"])

    assert result.exit_code == 1
    assert "failed to load" in result.output


def test_info_lists_connectors_and_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "jsondir, memory" in runner.invoke(app, ["info"]).output

    broken = EntryPoint(name="bad", value="no_such_module:X", group=registry.ENTRY_POINT_GROUP)
    monkeypatch.setattr(registry, "entry_points", lambda group: (broken,))
    assert "Broken connectors: bad" in runner.invoke(app, ["info"]).output
