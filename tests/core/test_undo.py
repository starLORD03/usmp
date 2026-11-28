from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

import pytest

from usmp.connectors.jsondir import JsonDirConnector
from usmp.connectors.memory import MemoryConnector, _MemoryWriter
from usmp.core.engine import migrate
from usmp.core.models import Item, UndoOutcome, UndoReport
from usmp.core.progress import WrittenItem, progress_path, read_progress_items
from usmp.core.undo import CHANGED, NOT_MIGRATED, ORIGIN_UNKNOWN, UndoError, undo
from usmp.sdk.connector import (
    ConnectorError,
    Removal,
    RemovalResult,
    Writer,
    WriteResult,
)

DOMAIN = {"domain": "demo"}


def folder(native_id: str = "f") -> Item:
    return Item.create(
        connector="src", native_id=native_id, domain="demo", type="folder", schema_version=1,
        payload={"name": "Work"},
    )  # fmt: skip


def note(n: int, parent: Item | None) -> Item:
    return Item.create(
        connector="src", native_id=f"n{n}", domain="demo", type="note", schema_version=1,
        payload={"text": f"Note {n}"}, parent=parent.usmp_id if parent else None,
    )  # fmt: skip


def source(n: int = 3) -> MemoryConnector:
    top = folder()
    return MemoryConnector(DOMAIN, items=[top, *(note(i, top) for i in range(n))])


def stored(target: MemoryConnector, usmp_id: str) -> str:
    return next(native for native, item in target.items.items() if item.usmp_id == usmp_id)


def edit(target: MemoryConnector, usmp_id: str, **payload: object) -> None:
    native = stored(target, usmp_id)
    old = target.items[native]
    target.items[native] = Item.create(
        usmp_id=old.usmp_id, connector="memory", native_id=native, domain=old.domain,
        type=old.type, schema_version=1, payload={**old.payload, **payload}, parent=old.parent,
    )  # fmt: skip


def test_undo_removes_everything_the_migration_created(tmp_path: Path) -> None:
    target = MemoryConnector(DOMAIN)
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")
    assert len(target.items) == 4

    result = undo(target, bundle_path=tmp_path / "a.usmp")

    assert result.report is not None and result.report.ok
    assert target.items == {}
    assert result.report.summary.removed == 4
    removed = [c for c in target.calls if c.startswith("remove:")]
    assert removed[-1] == f"remove:{folder().usmp_id}", "children before their folder"
    UndoReport.model_validate_json(result.report.model_dump_json())


def test_items_that_were_already_there_are_never_touched(tmp_path: Path) -> None:
    src, target = source(), MemoryConnector(DOMAIN)
    migrate(src, target, bundle_path=tmp_path / "first.usmp")

    again = undo(target, bundle_path=_rerun(src, target, tmp_path / "second.usmp"))

    assert again.report is not None
    assert again.report.summary.total == 0
    assert again.report.summary.left_alone == 4
    assert len(target.items) == 4
    assert not [c for c in target.calls if c.startswith("remove:")]


def _rerun(src: MemoryConnector, target: MemoryConnector, bundle: Path) -> Path:
    report = migrate(src, target, bundle_path=bundle).report
    assert report is not None and report.summary.already_present == 4
    return bundle


def test_items_changed_after_the_migration_are_kept_with_their_folder(tmp_path: Path) -> None:
    target = MemoryConnector(DOMAIN)
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")
    edited = note(1, folder()).usmp_id
    edit(target, edited, text="changed by the user")

    result = undo(target, bundle_path=tmp_path / "a.usmp")

    assert result.report is not None
    outcome = {r.usmp_id: r for r in result.report.items}
    assert outcome[edited].outcome is UndoOutcome.KEPT
    assert outcome[edited].reason == CHANGED
    assert outcome[folder().usmp_id].outcome is UndoOutcome.KEPT
    assert outcome[folder().usmp_id].reason == "it still holds items"
    assert result.report.summary.removed == 2
    assert {i.usmp_id for i in target.items.values()} == {edited, folder().usmp_id}


def test_items_the_user_added_keep_the_folder(tmp_path: Path) -> None:
    target = MemoryConnector(DOMAIN)
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")
    mine = note(99, None).model_copy(update={"parent": folder().usmp_id})
    target.items["mine"] = Item.model_validate(mine.model_dump())

    result = undo(target, bundle_path=tmp_path / "a.usmp")

    assert result.report is not None and result.report.summary.removed == 3
    assert set(target.items) == {"mine", stored(target, folder().usmp_id)}


def test_items_already_removed_are_reported(tmp_path: Path) -> None:
    target = MemoryConnector(DOMAIN)
    migrate(source(1), target, bundle_path=tmp_path / "a.usmp")
    del target.items[stored(target, note(0, folder()).usmp_id)]

    result = undo(target, bundle_path=tmp_path / "a.usmp")

    assert result.report is not None
    assert result.report.summary.already_gone == 1
    assert result.report.summary.removed == 1


class FailsAfter(MemoryConnector):
    """A memory target whose writes fail after ``limit`` items, before anything is verified."""

    def __init__(self, limit: int) -> None:
        super().__init__(DOMAIN)
        self.limit = limit

    def open_writer(self) -> Writer:
        connector = self

        class Failing(_MemoryWriter):
            def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
                for n, result in enumerate(super().write(items)):
                    if n == connector.limit:
                        raise KeyboardInterrupt
                    yield result

        return Failing(self)


def test_an_interrupted_migration_can_be_undone(tmp_path: Path) -> None:
    target = FailsAfter(2)
    with pytest.raises(KeyboardInterrupt):
        migrate(source(), target, bundle_path=tmp_path / "a.usmp")
    record = read_progress_items(tmp_path / "a.usmp")
    assert record is not None
    assert all(w.verified_hash is None for w in record[1].values())
    stopped = MemoryConnector(DOMAIN, items=target.items.values())
    stopped.items = dict(target.items)
    edit(stopped, note(0, folder()).usmp_id, text="changed")

    result = undo(stopped, bundle_path=tmp_path / "a.usmp")

    assert result.report is not None
    reasons = {r.usmp_id: r.reason for r in result.report.items}
    assert reasons[note(0, folder()).usmp_id] == NOT_MIGRATED
    assert result.report.summary.removed == 0, "the folder still holds the changed note"
    assert result.report.summary.kept == 2


def test_dry_run_and_cancel_remove_nothing(tmp_path: Path) -> None:
    target = MemoryConnector(DOMAIN)
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")

    dry = undo(target, bundle_path=tmp_path / "a.usmp", dry_run=True)
    cancelled = undo(target, bundle_path=tmp_path / "a.usmp", confirm=lambda _plan: False)

    assert dry.report is None and len(dry.plan.removals) == 4
    assert cancelled.cancelled and cancelled.report is None
    assert len(target.items) == 4


def test_items_of_unknown_origin_are_kept(tmp_path: Path) -> None:
    target = MemoryConnector(DOMAIN)
    migrate(source(1), target, bundle_path=tmp_path / "a.usmp")
    path = progress_path(tmp_path / "a.usmp")
    lines = path.read_text().splitlines()
    first = WrittenItem.model_validate_json(lines[1])
    path.write_text("\n".join([*lines, first.model_copy(update={"already_present": None})
                               .model_dump_json()]) + "\n")  # fmt: skip

    result = undo(target, bundle_path=tmp_path / "a.usmp")

    assert result.report is not None
    assert {r.usmp_id: r.reason for r in result.report.items}[first.usmp_id] == ORIGIN_UNKNOWN


def test_jsondir_target_is_undone(tmp_path: Path) -> None:
    target = JsonDirConnector({"path": str(tmp_path / "out"), **DOMAIN})
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")
    (tmp_path / "out" / "note" / "keep.json").write_text("not an item")

    result = undo(target, bundle_path=tmp_path / "a.usmp")

    assert result.report is not None and result.report.summary.removed == 4
    assert [p.name for p in (tmp_path / "out").rglob("*.json")] == ["keep.json"]


class TestRefusals:
    def test_without_a_progress_record(self, tmp_path: Path) -> None:
        target = MemoryConnector(DOMAIN)
        migrate(source(), target, bundle_path=tmp_path / "a.usmp")
        progress_path(tmp_path / "a.usmp").unlink()

        with pytest.raises(UndoError, match="no record of what this migration wrote"):
            undo(target, bundle_path=tmp_path / "a.usmp")

    def test_another_target(self, tmp_path: Path) -> None:
        target = JsonDirConnector({"path": str(tmp_path / "out"), **DOMAIN})
        migrate(source(), target, bundle_path=tmp_path / "a.usmp")

        with pytest.raises(UndoError, match="went to JSON directory, not In-memory"):
            undo(MemoryConnector(DOMAIN), bundle_path=tmp_path / "a.usmp")
        elsewhere = JsonDirConnector({"path": str(tmp_path / "other"), **DOMAIN})
        with pytest.raises(UndoError, match="different JSON directory location"):
            undo(elsewhere, bundle_path=tmp_path / "a.usmp")

    def test_the_source(self, tmp_path: Path) -> None:
        both = {"path": str(tmp_path / "same"), **DOMAIN}
        src = JsonDirConnector(both)
        list(src.open_writer().write([folder()]))
        migrate(src, JsonDirConnector(both), bundle_path=tmp_path / "a.usmp", confirm=_never)
        _fake_record(tmp_path / "a.usmp", both)

        with pytest.raises(UndoError, match="never touches a source"):
            undo(JsonDirConnector(both), bundle_path=tmp_path / "a.usmp")

    def test_a_target_that_cannot_remove(self, tmp_path: Path) -> None:
        target = NoRemove(DOMAIN)
        migrate(source(), target, bundle_path=tmp_path / "a.usmp")

        with pytest.raises(UndoError, match="cannot remove items"):
            undo(target, bundle_path=tmp_path / "a.usmp")


def _never(_plan: object) -> bool:
    return False


def _fake_record(bundle: Path, config: dict[str, str]) -> None:
    """A record of a migration whose source and target are the same place."""
    from usmp.core.bundle import BundleReader
    from usmp.core.models import ConnectorInfo
    from usmp.core.progress import ProgressLog

    with BundleReader(bundle) as reader:
        sha = reader.manifest.items_sha256
    info = ConnectorInfo(connector="jsondir", display_name="JSON directory")
    ProgressLog.start(
        bundle, items_sha256=sha, domain="demo", target=info, target_connector="jsondir",
        target_config=config, source_connector="jsondir", source_config=config,
    ).close()  # fmt: skip


class NoRemove(MemoryConnector):
    def open_writer(self) -> Writer:
        class Plain(Writer):
            def __init__(self, inner: Writer) -> None:
                self._inner = inner

            def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
                return self._inner.write(items)

            def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
                return self._inner.read_back(domain, native_ids)

        return Plain(_MemoryWriter(self))


def test_a_failure_while_removing_is_reported(tmp_path: Path) -> None:
    class Breaks(MemoryConnector):
        def open_writer(self) -> Writer:
            class Broken(_MemoryWriter):
                def remove(
                    self, domain: str, removals: Sequence[Removal]
                ) -> Iterator[RemovalResult]:
                    gen = super().remove(domain, removals)
                    yield next(gen)
                    raise ConnectorError("connection lost")

            return Broken(self)

    target = Breaks(DOMAIN)
    migrate(source(), target, bundle_path=tmp_path / "a.usmp")

    result = undo(target, bundle_path=tmp_path / "a.usmp")

    assert result.report is not None and not result.report.ok
    assert result.report.errors == ["removing stopped after 1 items: connection lost"]
    assert result.report.summary.removed == 1
    assert result.report.summary.kept == 3


def test_a_resumed_migration_is_undone_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from usmp.connectors.jsondir import _JsonDirWriter
    from usmp.core.engine import resume

    original = _JsonDirWriter.write

    def stops(self: _JsonDirWriter, items: Iterable[Item]) -> Iterator[WriteResult]:
        for n, result in enumerate(original(self, items)):
            if n == 2:
                raise KeyboardInterrupt
            yield result

    target = JsonDirConnector({"path": str(tmp_path / "out"), **DOMAIN})
    monkeypatch.setattr(_JsonDirWriter, "write", stops)
    with pytest.raises(KeyboardInterrupt):
        migrate(source(), target, bundle_path=tmp_path / "a.usmp")
    monkeypatch.setattr(_JsonDirWriter, "write", original)
    finished = resume(target, bundle_path=tmp_path / "a.usmp")
    assert finished.report is not None and finished.report.ok

    record = read_progress_items(tmp_path / "a.usmp")
    assert record is not None
    assert {w.already_present for w in record[1].values()} == {False}
    assert all(w.verified_hash for w in record[1].values())
    result = undo(target, bundle_path=tmp_path / "a.usmp")

    assert result.report is not None and result.report.summary.removed == 4
    assert list((tmp_path / "out").rglob("*.json")) == []
