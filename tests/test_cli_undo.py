import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from tests.test_cli_migrate import invoke, source_dir  # noqa: F401 - fixture
from usmp.cli import undo as undo_cli
from usmp.cli.app import app
from usmp.core.progress import progress_path

runner = CliRunner()


@pytest.fixture
def migrated(tmp_path: Path, source_dir: Path) -> Path:  # noqa: F811 - fixture
    result = invoke(tmp_path, source_dir, "--yes")
    assert result.exit_code == 0, result.output
    return tmp_path / "backup.usmp"


def run(tmp_path: Path, bundle: Path, *extra: str, answer: str | None = None) -> Result:
    args = ["undo", str(bundle), "--report-dir", str(tmp_path / "undo-reports"), *extra]
    return runner.invoke(app, args, input=answer)


def files(path: Path) -> list[str]:
    return sorted(p.name for p in path.rglob("*.json"))


def test_undo_removes_what_the_migration_wrote(tmp_path: Path, migrated: Path) -> None:
    edited = next((tmp_path / "new" / "event").glob("*.json"))
    data = json.loads(edited.read_text())
    data["payload"]["title"] = "Changed by me"
    from usmp.core.models import Item

    edited.write_text(Item.create(
        usmp_id=data["usmp_id"], connector="x", native_id="x", domain="demo", type="event",
        schema_version=1, payload=data["payload"], parent=data.get("parent"),
    ).model_dump_json())  # fmt: skip

    result = run(tmp_path, migrated, "--yes")

    assert result.exit_code == 0, result.output
    assert "Undo finished" in result.output
    assert "Removed" in result.output and "Kept" in result.output
    assert edited.is_file() and len(files(tmp_path / "new")) == 2, "the event and its calendar"
    [report_json] = (tmp_path / "undo-reports").glob("*.json")
    report = json.loads(report_json.read_text())
    assert report["summary"]["removed"] == 2 and report["summary"]["kept"] == 2
    assert (
        "changed on the target"
        in (tmp_path / "undo-reports" / report_json.stem).with_suffix(".md").read_text()
    )


def test_dry_run_removes_nothing(tmp_path: Path, migrated: Path) -> None:
    result = run(tmp_path, migrated, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "Undo preview" in result.output and "Dry run" in result.output
    assert len(files(tmp_path / "new")) == 4


def test_needs_yes_when_not_interactive(tmp_path: Path, migrated: Path) -> None:
    result = run(tmp_path, migrated)

    assert result.exit_code == 3, result.output
    assert "pass --yes" in result.output
    assert len(files(tmp_path / "new")) == 4


def test_asks_before_removing(
    tmp_path: Path, migrated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(undo_cli, "_interactive", lambda: True)

    declined = run(tmp_path, migrated, answer="n\n")
    accepted = run(tmp_path, migrated, answer="y\n")

    assert declined.exit_code == 3 and "nothing was removed" in declined.output.lower()
    assert accepted.exit_code == 0, accepted.output
    assert files(tmp_path / "new") == []


def test_without_a_record_nothing_is_removed(tmp_path: Path, migrated: Path) -> None:
    progress_path(migrated).unlink()

    result = run(tmp_path, migrated, "--yes")

    assert result.exit_code == 2
    assert "no record of what this migration wrote" in " ".join(result.output.split())
    assert len(files(tmp_path / "new")) == 4


def test_another_target_is_refused(tmp_path: Path, migrated: Path) -> None:
    result = run(tmp_path, migrated, "--yes", "--to", f"jsondir:path={tmp_path / 'other'}")

    assert result.exit_code == 2
    assert "different JSON directory location" in " ".join(result.output.split())


def test_a_failure_while_removing_is_reported(
    tmp_path: Path, migrated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from usmp.connectors.jsondir import _JsonDirWriter
    from usmp.sdk.connector import ConnectorError

    def broken(self: object, domain: str, removals: object) -> object:
        raise ConnectorError("disk full")
        yield  # pragma: no cover

    monkeypatch.setattr(_JsonDirWriter, "remove", broken)

    result = run(tmp_path, migrated, "--yes")

    assert result.exit_code == 1, result.output
    assert "Undo stopped early" in result.output and "disk full" in result.output
