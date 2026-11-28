from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from tests.conftest import make_capabilities, make_event
from tests.core.test_resume import FIELDS, StopsAfter, source, target
from usmp.connectors.memory import MemoryConnector, _MemoryWriter
from usmp.core.engine import MigrationError, migrate, resume
from usmp.core.models import Item, LossReason, LossRecord
from usmp.core.progress import ProgressLog, progress_path, read_progress
from usmp.sdk.connector import Writer, WriteResult

PASSPHRASE = "correct horse battery"  # noqa: S105 - test value
WRONG = "not the right one"
SECRET_DETAIL = "room5@rooms.example.com: stored as a resource"  # noqa: S105 - not a password


class Converts(MemoryConnector):
    """A target that reports every item as stored with a changed (declared) field."""

    def __init__(self) -> None:
        super().__init__(capabilities=make_capabilities())

    def open_writer(self) -> Writer:
        class Writes(_MemoryWriter):
            def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
                for result in super().write(items):
                    loss = LossRecord(
                        usmp_id=result.usmp_id,
                        field="title",
                        reason=LossReason.VALUE_CONVERTED,
                        stage="import",
                        detail=SECRET_DETAIL,
                    )
                    yield WriteResult(result.usmp_id, result.native_id, (loss,))

        return Writes(self)


def test_encrypted_backup_and_report(tmp_path: Path) -> None:
    bundle = tmp_path / "b.usmp"

    result = migrate(source(), Converts(), bundle_path=bundle, passphrase=PASSPHRASE)

    assert bundle.read_bytes().startswith(b"age-encryption.org/v1\n")
    assert result.preview.encrypted
    assert result.report is not None and result.report.backup_encrypted
    # The report keeps loss details; the progress record next to the backup does not.
    assert any(loss.detail == SECRET_DETAIL for loss in result.report.losses)
    record = progress_path(bundle).read_text()
    assert SECRET_DETAIL not in record
    assert FIELDS["title"] not in record


def test_plain_backup_is_reported_as_plain(tmp_path: Path) -> None:
    result = migrate(source(), target(), bundle_path=tmp_path / "b.usmp")
    assert result.report is not None and result.report.backup_encrypted is False
    assert not result.preview.encrypted


def test_resume_an_encrypted_migration(tmp_path: Path) -> None:
    bundle = tmp_path / "b.usmp"
    dst = StopsAfter(1)
    with pytest.raises(KeyboardInterrupt):
        migrate(source(), dst, bundle_path=bundle, passphrase=PASSPHRASE)

    with pytest.raises(MigrationError, match="encrypted: give its passphrase"):
        resume(dst, bundle_path=bundle)
    with pytest.raises(MigrationError, match="wrong passphrase"):
        resume(dst, bundle_path=bundle, passphrase=WRONG)

    dst.limit = 99
    done = resume(dst, bundle_path=bundle, passphrase=PASSPHRASE)

    assert done.report is not None and done.report.ok, done.report
    assert done.report.backup_encrypted
    assert len(dst.items) == 3


def test_private_progress_record_drops_loss_details(tmp_path: Path) -> None:
    from usmp.core.models import ConnectorInfo

    item = make_event("1")
    loss = LossRecord(
        usmp_id=item.usmp_id,
        field="title",
        reason=LossReason.VALUE_CONVERTED,
        stage="import",
        detail=SECRET_DETAIL,
    )
    with ProgressLog.start(
        tmp_path / "b.usmp",
        items_sha256="0" * 64,
        domain="demo",
        target=ConnectorInfo(connector="memory", display_name="Memory"),
        target_connector="memory",
        target_config={},
        private=True,
    ) as log:
        log.record(WriteResult(item.usmp_id, "n1", (loss,)))

    record = read_progress(tmp_path / "b.usmp")
    assert record is not None
    [kept] = record[1][item.usmp_id].losses
    assert kept.detail == "" and kept.field == "title"
