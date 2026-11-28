import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from tests.conftest import make_capabilities, make_event
from usmp.connectors.memory import MemoryConnector, _MemoryWriter
from usmp.core.engine import MigrationError, migrate, resume
from usmp.core.models import Item, ItemStatus
from usmp.core.progress import ProgressError, progress_path, read_progress, safe_config
from usmp.sdk.connector import ConnectorError, Writer, WriteResult

FIELDS = {"title": "Standup", "start": "2026-01-05T09:00:00Z"}


def source(n: int = 3) -> MemoryConnector:
    items = [make_event(str(i), **FIELDS) for i in range(n)]
    return MemoryConnector(capabilities=make_capabilities(), items=items)


def target() -> MemoryConnector:
    return MemoryConnector(capabilities=make_capabilities())


class StopsAfter(MemoryConnector):
    """A memory target whose writes are interrupted (like Ctrl+C) after ``limit`` items."""

    def __init__(self, limit: int) -> None:
        super().__init__(capabilities=make_capabilities())
        self.limit = limit

    def open_writer(self) -> Writer:
        connector = self

        class Interrupted(_MemoryWriter):
            def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
                for n, result in enumerate(super().write(items)):
                    if n == connector.limit:
                        raise KeyboardInterrupt
                    yield result

        return Interrupted(self)


def test_rerun_writes_nothing_and_verifies_everything(tmp_path: Path) -> None:
    src, dst = source(), target()
    migrate(src, dst, bundle_path=tmp_path / "a.usmp")

    again = migrate(src, dst, bundle_path=tmp_path / "b.usmp")

    assert again.report is not None and again.report.ok
    assert len(dst.items) == 3
    assert all(r.already_present for r in again.report.items)
    assert again.report.summary.already_present == 3
    assert again.report.summary.verified == 3


def test_interrupted_run_resumes_without_duplicates(tmp_path: Path) -> None:
    bundle = tmp_path / "b.usmp"
    dst = StopsAfter(2)
    with pytest.raises(KeyboardInterrupt):
        migrate(source(), dst, bundle_path=bundle)
    record = read_progress(bundle)
    assert record is not None and len(record[1]) == 2

    # The source is not needed any more: everything comes from the backup.
    dst.limit = 99
    done = resume(dst, bundle_path=bundle)
    assert done.report is not None and done.report.ok, done.report
    assert len(dst.items) == 3
    # The third item was stored just before the interruption but never recorded: the target's
    # own record of it (its usmp_id) stops it being written twice.
    assert done.report.summary.already_present == 3
    assert done.report.summary.verified == 3
    record = read_progress(bundle)
    assert record is not None and len(record[1]) == 3


def test_resume_of_finished_run_writes_nothing(tmp_path: Path) -> None:
    bundle, dst = tmp_path / "b.usmp", target()
    migrate(source(), dst, bundle_path=bundle)

    result = resume(dst, bundle_path=bundle)

    assert result.report is not None and result.report.ok
    assert result.report.summary.already_present == 3
    assert len(dst.items) == 3


class TrustingTarget(MemoryConnector):
    """A target whose writer keeps the SDK's default ``already_written``."""

    def open_writer(self) -> Writer:
        writer = super().open_writer()
        writer.already_written = Writer.already_written.__get__(writer)  # type: ignore[method-assign]
        return writer


def test_default_writer_trusts_the_progress_record(tmp_path: Path) -> None:
    bundle, dst = tmp_path / "b.usmp", TrustingTarget(capabilities=make_capabilities())
    migrate(source(), dst, bundle_path=bundle)

    result = resume(dst, bundle_path=bundle)

    assert result.report is not None and result.report.ok
    assert len(dst.items) == 3
    assert result.report.summary.already_present == 3


def test_resume_without_progress_imports_the_backup(tmp_path: Path) -> None:
    bundle = tmp_path / "b.usmp"
    migrate(source(), target(), bundle_path=bundle, dry_run=True)
    assert read_progress(bundle) is None

    dst = target()
    result = resume(dst, bundle_path=bundle)

    assert result.report is not None and result.report.ok
    assert result.report.source.connector == "memory"
    assert len(dst.items) == 3
    assert read_progress(bundle) is not None


def test_resume_dry_run_and_cancel_write_nothing(tmp_path: Path) -> None:
    bundle, dst = tmp_path / "b.usmp", target()
    migrate(source(), target(), bundle_path=bundle, dry_run=True)

    dry = resume(dst, bundle_path=bundle, dry_run=True)
    cancelled = resume(dst, bundle_path=bundle, confirm=lambda _p: False)

    assert dry.report is None and dry.preview.total == 3
    assert cancelled.cancelled
    assert dst.items == {}


def test_resume_refuses_another_backup_or_target(tmp_path: Path) -> None:
    a, b = tmp_path / "a.usmp", tmp_path / "b.usmp"
    migrate(source(1), target(), bundle_path=a)
    migrate(source(2), target(), bundle_path=b)
    progress_path(a).write_bytes(progress_path(b).read_bytes())

    with pytest.raises(MigrationError, match="belongs to a different backup"):
        resume(target(), bundle_path=a)

    class Other(MemoryConnector):
        name = "other"

    with pytest.raises(MigrationError, match="was going to In-memory store"):
        resume(Other(capabilities=make_capabilities("other")), bundle_path=b)


def test_resume_refuses_a_damaged_backup(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="not a readable bundle"):
        resume(target(), bundle_path=tmp_path / "missing.usmp")


def test_checking_the_target_fails_safely(tmp_path: Path) -> None:
    class Blind(MemoryConnector):
        def open_writer(self) -> Writer:
            writer = super().open_writer()

            def fail(
                items: Sequence[Item], known: Mapping[str, WriteResult]
            ) -> Iterator[WriteResult]:
                raise ConnectorError("offline")

            writer.already_written = fail  # type: ignore[method-assign]
            return writer

    dst = Blind(capabilities=make_capabilities())
    result = migrate(source(), dst, bundle_path=tmp_path / "b.usmp")

    assert result.report is not None and not result.report.ok
    assert result.report.errors == ["cannot check what is already in the target: offline"]
    assert dst.items == {}


class TestProgressRecord:
    def test_record_has_no_secrets_and_private_permissions(self, tmp_path: Path) -> None:
        bundle = tmp_path / "b.usmp"
        dst = MemoryConnector({"domain": "demo"}, capabilities=make_capabilities())
        migrate(source(), dst, bundle_path=bundle)

        path = progress_path(bundle)
        header = json.loads(path.read_text().splitlines()[0])
        assert header["format"] == "usmp-progress"
        assert header["target_config"] == {"domain": "demo"}
        if os.name == "posix":
            assert path.stat().st_mode & 0o077 == 0

    def test_safe_config_drops_secret_keys(self) -> None:
        config = {"url": "u", "password": "p", "password_env": "PW", "api_token": "t", "x_key": "k"}
        assert safe_config(config) == {"url": "u", "password_env": "PW"}

    def test_damaged_last_line_is_ignored(self, tmp_path: Path) -> None:
        bundle = tmp_path / "b.usmp"
        migrate(source(), target(), bundle_path=bundle)
        path = progress_path(bundle)
        path.write_text(path.read_text() + '{"usmp_id": "usm')

        record = read_progress(bundle)

        assert record is not None and len(record[1]) == 3

    def test_damaged_middle_line_is_an_error(self, tmp_path: Path) -> None:
        bundle = tmp_path / "b.usmp"
        migrate(source(), target(), bundle_path=bundle)
        path = progress_path(bundle)
        lines = path.read_text().splitlines()
        path.write_text("\n".join([lines[0], "garbage", *lines[1:]]) + "\n")

        with pytest.raises(ProgressError, match="line 2 is damaged"):
            read_progress(bundle)
        with pytest.raises(MigrationError, match="line 2 is damaged"):
            resume(target(), bundle_path=bundle)

    @pytest.mark.parametrize(
        ("content", "message"), [(b"", "is empty"), (b"{}\n", "not a USMP progress record")]
    )
    def test_unusable_records(self, tmp_path: Path, content: bytes, message: str) -> None:
        bundle = tmp_path / "b.usmp"
        progress_path(bundle).write_bytes(content)
        with pytest.raises(ProgressError, match=message):
            read_progress(bundle)

    def test_unreadable_record(self, tmp_path: Path) -> None:
        bundle = tmp_path / "b.usmp"
        progress_path(bundle).mkdir()
        with pytest.raises(ProgressError, match="cannot read"):
            read_progress(bundle)


def test_statuses_of_resumed_items_are_real_verifications(tmp_path: Path) -> None:
    bundle, dst = tmp_path / "b.usmp", target()
    migrate(source(), dst, bundle_path=bundle)
    # Someone edits one copy on the target after the first run: it is never overwritten.
    native_id, item = next(iter(dst.items.items()))
    dst.items[native_id] = Item.create(
        usmp_id=item.usmp_id,
        connector="memory",
        native_id=native_id,
        domain="demo",
        type="event",
        schema_version=1,
        payload={**item.payload, "title": "Edited"},
    )

    result = resume(dst, bundle_path=bundle)

    assert result.report is not None and not result.report.ok
    statuses = sorted(r.status for r in result.report.items)
    assert statuses.count(ItemStatus.MISMATCH) == 1
    assert dst.items[native_id].payload["title"] == "Edited"
