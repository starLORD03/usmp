from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import make_event
from usmp.cli import migrate as migrate_cli
from usmp.cli.app import app
from usmp.connectors.jsondir import JsonDirConnector
from usmp.core.models import Item

runner = CliRunner()


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    path = tmp_path / "old"
    cal = Item.create(
        connector="seed",
        native_id="cal",
        domain="calendar",
        type="calendar",
        schema_version=1,
        payload={"name": "Work"},
    )
    events = [
        Item.create(
            connector="seed",
            native_id=f"e{n}",
            domain="calendar",
            type="event",
            schema_version=1,
            parent=cal.usmp_id,
            payload={"title": f"Event {n}"},
        )
        for n in range(3)
    ]
    writer = JsonDirConnector({"path": str(path), "domain": "calendar"}).open_writer()
    list(writer.write([cal, *events]))
    return path


def invoke(tmp_path: Path, source_dir: Path, *extra: str):  # type: ignore[no-untyped-def]
    return runner.invoke(
        app,
        [
            "migrate",
            "--from",
            f"jsondir:path={source_dir},label=Old",
            "--to",
            f"jsondir:path={tmp_path / 'new'}",
            "--domain",
            "calendar",
            "--bundle",
            str(tmp_path / "backup.usmp"),
            "--report-dir",
            str(tmp_path / "reports"),
            *extra,
        ],
    )


def test_end_to_end_jsondir_to_jsondir(tmp_path: Path, source_dir: Path) -> None:
    result = invoke(tmp_path, source_dir, "--yes")

    assert result.exit_code == 0, result.output
    assert "Migration verified" in result.output
    assert "Nothing will be lost" in result.output
    assert len(list((tmp_path / "new").glob("*/*.json"))) == 4
    assert len(list((tmp_path / "reports").iterdir())) == 3
    assert (tmp_path / "backup.usmp").exists()
    bundle_check = runner.invoke(app, ["bundle", "validate", str(tmp_path / "backup.usmp")])
    assert bundle_check.exit_code == 0


def test_dry_run_writes_nothing(tmp_path: Path, source_dir: Path) -> None:
    result = invoke(tmp_path, source_dir, "--dry-run")

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert not (tmp_path / "new").exists()


def test_non_interactive_without_yes_is_cancelled(tmp_path: Path, source_dir: Path) -> None:
    result = invoke(tmp_path, source_dir)

    assert result.exit_code == migrate_cli.EXIT_CANCELLED
    assert "pass --yes" in result.output
    assert not (tmp_path / "new").exists()


def test_interactive_confirmation(
    tmp_path: Path, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrate_cli, "_interactive", lambda: True)

    declined = runner.invoke(
        app,
        [
            "migrate",
            "--from",
            f"jsondir:path={source_dir}",
            "--to",
            f"jsondir:path={tmp_path / 'new'}",
            "--domain",
            "calendar",
            "--bundle",
            str(tmp_path / "b.usmp"),
        ],
        input="n\n",
    )

    assert declined.exit_code == migrate_cli.EXIT_CANCELLED
    assert "Write 4 items to the target?" in declined.output
    assert not (tmp_path / "new").exists()


def test_rerun_into_same_target_is_reported_not_verified(tmp_path: Path, source_dir: Path) -> None:
    assert invoke(tmp_path, source_dir, "--yes").exit_code == 0

    second = invoke(tmp_path, source_dir, "--yes")

    assert second.exit_code == migrate_cli.EXIT_NOT_VERIFIED
    assert "refusing to overwrite" in second.output


def test_preview_explains_losses_and_blocked_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    from tests.conftest import make_capabilities
    from usmp.connectors.memory import MemoryConnector
    from usmp.core.engine import migrate

    todo = Item.create(
        connector="memory",
        native_id="t",
        domain="calendar",
        type="todo",
        schema_version=1,
        payload={"title": "x"},
    )
    source = MemoryConnector(
        capabilities=make_capabilities(), items=[make_event("1", title="T", location="L"), todo]
    )
    source_caps = source.capabilities().model_copy(deep=True)
    source_caps.domains[0].types.append(
        source_caps.domains[0].types[0].model_copy(update={"type": "todo"})
    )
    source._capabilities = source_caps
    target = MemoryConnector(capabilities=make_capabilities(write=["title"]))
    preview = migrate(source, target, bundle_path=tmp_path / "b.usmp", dry_run=True).preview

    console = Console(record=True, width=120)
    monkeypatch.setattr(migrate_cli, "console", console)
    migrate_cli._show_preview(preview)
    out = console.export_text()

    assert "Some data cannot be carried over" in out
    assert "whole item" in out
    assert "location" in out
    assert "1 item(s) cannot be stored by the target at all" in out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--from", "nope", "--to", "memory"], "unknown connector 'nope'"),
        (
            [
                "--from",
                "jsondir:path=/does/not/exist,domain=calendar",
                "--to",
                "memory:domain=calendar",
            ],
            "is not a directory",
        ),
        (
            ["--from", "memory:domain=calendar", "--to", "memory:domain=contacts"],
            "cannot write 'calendar'",
        ),
    ],
)
def test_errors_exit_2(tmp_path: Path, args: list[str], message: str) -> None:
    result = runner.invoke(app, ["migrate", *args, "--bundle", str(tmp_path / "b.usmp")])

    assert result.exit_code == migrate_cli.EXIT_ERROR, result.output
    assert message in result.output
