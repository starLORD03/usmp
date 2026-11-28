from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from tests.conftest import make_event
from tests.core.test_selection import calendar_source
from usmp.cli import migrate as migrate_cli
from usmp.cli.app import app
from usmp.connectors.jsondir import JsonDirConnector, _JsonDirWriter
from usmp.core.models import Item
from usmp.sdk.connector import ConnectorError, WriteResult

runner = CliRunner()


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    path = tmp_path / "old"
    cal = Item.create(
        connector="seed",
        native_id="cal",
        domain="demo",
        type="demo",
        schema_version=1,
        payload={"name": "Work"},
    )
    events = [
        Item.create(
            connector="seed",
            native_id=f"e{n}",
            domain="demo",
            type="event",
            schema_version=1,
            parent=cal.usmp_id,
            payload={"title": f"Event {n}"},
        )
        for n in range(3)
    ]
    writer = JsonDirConnector({"path": str(path), "domain": "demo"}).open_writer()
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
            "demo",
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
            "demo",
            "--bundle",
            str(tmp_path / "b.usmp"),
        ],
        input="n\n",
    )

    assert declined.exit_code == migrate_cli.EXIT_CANCELLED
    assert "Write 4 items to the target?" in declined.output
    assert not (tmp_path / "new").exists()


def text(result: Result) -> str:
    """Output with Rich's line wrapping undone."""
    return " ".join(result.output.split())


def test_rerun_into_same_target_writes_nothing_twice(tmp_path: Path, source_dir: Path) -> None:
    assert invoke(tmp_path, source_dir, "--yes").exit_code == 0

    second = invoke(tmp_path, source_dir, "--yes")

    assert second.exit_code == migrate_cli.EXIT_OK, second.output
    assert "4 item(s) were already in the target" in text(second)
    assert len(list((tmp_path / "new").rglob("*.json"))) == 4


def stop_writing(monkeypatch: pytest.MonkeyPatch, after: int, error: BaseException) -> None:
    original = _JsonDirWriter.write

    def write(self: _JsonDirWriter, items: Iterable[Item]) -> Iterator[WriteResult]:
        for n, result in enumerate(original(self, items)):
            if n == after:
                raise error
            yield result

    monkeypatch.setattr(_JsonDirWriter, "write", write)


def resume_cli(tmp_path: Path, *extra: str):  # type: ignore[no-untyped-def]
    return runner.invoke(
        app,
        [
            "migrate",
            "--resume",
            str(tmp_path / "backup.usmp"),
            "--report-dir",
            str(tmp_path / "reports"),
            *extra,
        ],
    )


def test_interrupted_migration_can_be_resumed(
    tmp_path: Path, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        stop_writing(patch, 2, KeyboardInterrupt())
        stopped = invoke(tmp_path, source_dir, "--yes")
    assert stopped.exit_code == migrate_cli.EXIT_CANCELLED
    assert "usmp migrate --resume" in text(stopped)

    resumed = resume_cli(tmp_path, "--yes")

    assert resumed.exit_code == migrate_cli.EXIT_OK, resumed.output
    assert "already in the target" in text(resumed)
    assert len(list((tmp_path / "new").rglob("*.json"))) == 4


def test_failed_write_suggests_resume(
    tmp_path: Path, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop_writing(monkeypatch, 1, ConnectorError("disk full"))

    result = invoke(tmp_path, source_dir, "--yes")

    assert result.exit_code == migrate_cli.EXIT_NOT_VERIFIED
    assert "disk full" in text(result)
    assert "usmp migrate --resume" in text(result)


def test_stopped_before_writing_says_nothing_was_written(
    tmp_path: Path, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(migrate_cli, "migrate", interrupted)

    result = invoke(tmp_path, source_dir, "--yes")

    assert result.exit_code == migrate_cli.EXIT_CANCELLED
    assert "nothing was written" in text(result)


def test_resume_imports_a_backup_into_a_new_target(tmp_path: Path, source_dir: Path) -> None:
    assert invoke(tmp_path, source_dir, "--dry-run").exit_code == 0

    missing_target = resume_cli(tmp_path, "--yes")
    imported = resume_cli(tmp_path, "--yes", "--to", f"jsondir:path={tmp_path / 'other'}")

    assert missing_target.exit_code == migrate_cli.EXIT_ERROR
    assert "give --to" in text(missing_target)
    assert imported.exit_code == migrate_cli.EXIT_OK, imported.output
    assert len(list((tmp_path / "other").rglob("*.json"))) == 4


def test_resume_problems_are_explained(tmp_path: Path, source_dir: Path) -> None:
    assert invoke(tmp_path, source_dir, "--yes").exit_code == 0

    with_source = resume_cli(tmp_path, "--from", "jsondir:path=x")
    dry = resume_cli(tmp_path, "--dry-run")
    other = resume_cli(tmp_path, "--to", "memory:")

    assert with_source.exit_code == migrate_cli.EXIT_ERROR
    assert "leave out --from" in text(with_source)
    assert dry.exit_code == 0 and "nothing was written" in text(dry)
    assert other.exit_code == migrate_cli.EXIT_ERROR
    assert "Nothing more was written" in text(other)


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
        domain="demo",
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
                "jsondir:path=/does/not/exist,domain=demo",
                "--to",
                "memory:domain=demo",
            ],
            "is not a directory",
        ),
        (
            ["--from", "memory:domain=demo", "--to", "memory:domain=contacts"],
            "cannot write 'demo'",
        ),
    ],
)
def test_errors_exit_2(tmp_path: Path, args: list[str], message: str) -> None:
    result = runner.invoke(app, ["migrate", *args, "--bundle", str(tmp_path / "b.usmp")])

    assert result.exit_code == migrate_cli.EXIT_ERROR, result.output
    assert message in result.output


def test_since_and_calendar_move_only_part(tmp_path: Path) -> None:
    source = calendar_source(tmp_path)
    args = ["--from", f"ics:path={source.config['path']}", "--to", f"ics:path={tmp_path / 'new'}"]
    args += ["--bundle", str(tmp_path / "b.usmp"), "--report-dir", str(tmp_path / "reports")]

    result = runner.invoke(
        app, ["migrate", *args, "--since", "2026-01-01", "--calendar", "work", "--yes"]
    )

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "Only: from 2026-01-01; calendars: work" in output
    assert "3 item(s) left out (they stay in the backup)" in output
    assert "3 item(s) were outside the chosen selection" in output
    [markdown] = (tmp_path / "reports").glob("*.md")
    assert "| Selection | from 2026-01-01; calendars: work |" in markdown.read_text()

    again = runner.invoke(
        app, ["migrate", "--resume", str(tmp_path / "b.usmp"), "--until", "2026-12-31", "--yes"]
    )
    assert again.exit_code == 2
    assert "different selection" in again.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--since", "tomorrow"], "'tomorrow' is not a date"),
        (["--since", "2026-02-01", "--until", "2026-01-01"], "since must be earlier than until"),
    ],
)
def test_bad_selection_is_explained(tmp_path: Path, args: list[str], message: str) -> None:
    result = runner.invoke(app, ["migrate", "--from", "ics:path=x", "--to", "ics:path=y", *args])
    assert result.exit_code == 2
    assert message in result.output


def test_preview_lists_odd_source_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.console import Console

    from tests.domains.test_calendar_checks import BACKWARDS, CAL
    from usmp.connectors.memory import MemoryConnector
    from usmp.core.engine import migrate
    from usmp.domains.calendar import event_item

    def long(n: int) -> Item:
        payload = {"uid": f"l{n}@x", "start": "2026-03-02T09:00:00", "end": "2026-04-02T09:00:00"}
        return event_item("s", f"l{n}", payload, parent=CAL.usmp_id)

    source = MemoryConnector(
        {"domain": "calendar"}, items=[CAL, BACKWARDS, *(long(n) for n in range(6))]
    )
    target = MemoryConnector({"domain": "calendar"})
    preview = migrate(source, target, bundle_path=tmp_path / "b.usmp", dry_run=True).preview

    console = Console(record=True, width=120)
    monkeypatch.setattr(migrate_cli, "console", console)
    migrate_cli._show_preview(preview)
    out = console.export_text()

    assert "7 item(s) look wrong in the source" in out
    assert "starts 2025-02-16T14:45:00 (UTC): ends 2 hours before it starts" in out
    assert "lasts 31 days: check its end" in out
    assert "… and 2 more, all listed in the report." in out


def test_progress_bar_in_a_terminal(
    tmp_path: Path, source_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from rich.console import Console

    screen = io.StringIO()
    monkeypatch.setattr(
        migrate_cli, "console", Console(file=screen, force_terminal=True, width=100)
    )

    result = invoke(tmp_path, source_dir, "--yes")

    assert result.exit_code == 0, result.output
    assert "Writing" in screen.getvalue()
    assert "4/4" in screen.getvalue()


def test_progress_bar_stays_out_of_plain_output() -> None:
    bar = migrate_cli._WriteBar()
    bar(0, 10)
    bar(5, 10)
    bar.close()

    assert bar._bar is None


def test_progress_bar_closes_when_writing_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from rich.console import Console

    monkeypatch.setattr(migrate_cli, "console", Console(file=io.StringIO(), force_terminal=True))
    bar = migrate_cli._WriteBar()
    bar(0, 0)
    assert bar._bar is None, "nothing to write: no bar"
    bar(1, 10)
    assert bar._bar is not None
    bar.close()
    assert bar._bar is None
