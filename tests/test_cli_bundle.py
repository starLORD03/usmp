from pathlib import Path

from typer.testing import CliRunner

from tests.conftest import make_event
from usmp.cli.app import app
from usmp.core.bundle import BundleWriter
from usmp.core.models import CapabilityManifest, ConnectorInfo

runner = CliRunner()


def write_bundle(path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest) -> Path:
    with BundleWriter(path, source=source_info, capabilities=capabilities) as w:
        w.add_item(make_event("1"))
        w.add_item(make_event("2"))
    return path


def test_inspect(
    tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
) -> None:
    path = write_bundle(tmp_path / "b.usmp", source_info, capabilities)

    result = runner.invoke(app, ["bundle", "inspect", str(path)])

    assert result.exit_code == 0, result.output
    assert "Memory (test calendar)" in result.output
    assert "event" in result.output
    assert "2" in result.output


def test_inspect_json(
    tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
) -> None:
    path = write_bundle(tmp_path / "b.usmp", source_info, capabilities)

    result = runner.invoke(app, ["bundle", "inspect", "--json", str(path)])

    assert result.exit_code == 0
    assert '"format": "usmp-bundle"' in result.output


def test_validate_ok(
    tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
) -> None:
    path = write_bundle(tmp_path / "b.usmp", source_info, capabilities)

    result = runner.invoke(app, ["bundle", "validate", str(path)])

    assert result.exit_code == 0
    assert "is valid" in result.output


def test_validate_reports_problems(
    tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
) -> None:
    import zipfile

    good = write_bundle(tmp_path / "b.usmp", source_info, capabilities)
    bad = tmp_path / "bad.usmp"
    with zipfile.ZipFile(good) as zin, zipfile.ZipFile(bad, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info)
            zout.writestr(info, b"" if info.filename == "items.jsonl" else data)

    result = runner.invoke(app, ["bundle", "validate", str(bad)])

    assert result.exit_code == 1
    assert "problem" in result.output


def test_unreadable_bundle_exits_2(tmp_path: Path) -> None:
    path = tmp_path / "x.usmp"
    path.write_text("not a zip")

    result = runner.invoke(app, ["bundle", "inspect", str(path)])

    assert result.exit_code == 2
    assert "Cannot open bundle" in result.output
