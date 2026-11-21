import subprocess
import sys

import pytest
from typer.testing import CliRunner

from usmp import __version__
from usmp.cli.app import app

runner = CliRunner()


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_version(flag: str) -> None:
    result = runner.invoke(app, [flag])

    assert result.exit_code == 0
    assert result.output.strip() == f"usmp {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])

    assert "Usage" in result.output
    assert "connectors" in result.output


def test_info_reports_version() -> None:
    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0
    assert __version__ in result.output


def test_runs_as_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "usmp", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == f"usmp {__version__}"
