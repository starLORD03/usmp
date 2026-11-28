"""Progress records: what a migration has written so far, so an interrupted run can resume.

The record is a JSON Lines file next to the backup bundle (``<bundle>.progress.jsonl``). It is not
part of the bundle, which stays an unchanging snapshot of the source (spec §7). The first line
describes the run; every further line is one item the target confirmed as stored. Lines are
flushed as they are written, so a run stopped at any point (Ctrl+C, a crash, a lost connection)
leaves a usable record. A damaged last line, from a run killed mid-write, is ignored.

Records hold item ids, target ids and loss details, never credentials: target configuration keys
that look like secrets are left out.
"""

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from usmp.core.models import ConnectorInfo, LossRecord, Selection
from usmp.sdk.connector import WriteResult

PROGRESS_SUFFIX = ".progress.jsonl"
# Keys naming an environment variable (password_env) are fine; the value itself never is.
_SECRET_KEY = re.compile(r"pass|secret|token|credential|auth|key", re.IGNORECASE)
# A password written into a URL (https://user:pass@host) is dropped, keeping the user name.
_URL_PASSWORD = re.compile(r"(?<=://)([^/@:\s]*):[^/@\s]*@")


class ProgressError(Exception):
    """The progress record is missing, unreadable or belongs to another backup."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProgressHeader(_Model):
    format: Literal["usmp-progress"] = "usmp-progress"
    format_version: Literal[1] = 1
    started_at: AwareDatetime
    items_sha256: str
    """Ties the record to one bundle: ``manifest.items_sha256`` of that bundle."""
    domain: str
    target: ConnectorInfo
    target_connector: str
    target_config: dict[str, str]
    selection: Selection | None = None
    """The part of the backup being migrated; a resumed run keeps it."""
    source_connector: str | None = None
    source_config: dict[str, str] | None = None
    """Where the migration read from (without secrets); undo refuses to touch it."""
    target_checked: bool = False
    """The target was searched for items already there before anything was written. Items found
    there later, but missing from the record, were written by this migration."""


class WrittenItem(_Model):
    """One item on the target. A later line for the same ``usmp_id`` replaces an earlier one."""

    usmp_id: str
    native_id: str
    losses: list[LossRecord] = []
    already_present: bool | None = None
    """True if the item was on the target before this migration (it was not written), false if
    this migration wrote it, absent if unknown. Undo only removes items with ``false``."""
    created: bool = False
    """``WriteResult.created``: the writer made a new container for this item."""
    verified_hash: str | None = None
    """``content_hash`` of the item as read back and verified on the target."""

    def result(self) -> WriteResult:
        return WriteResult(self.usmp_id, self.native_id, tuple(self.losses), self.created)


def progress_path(bundle_path: Path) -> Path:
    return bundle_path.with_name(bundle_path.name + PROGRESS_SUFFIX)


def safe_config(config: Mapping[str, str]) -> dict[str, str]:
    """``config`` without anything that could be a secret."""
    return {
        k: _URL_PASSWORD.sub(r"\1@", v)
        for k, v in config.items()
        if k.endswith("_env") or not _SECRET_KEY.search(k)
    }


def read_progress(bundle_path: Path) -> tuple[ProgressHeader, dict[str, WriteResult]] | None:
    """The header and written items of the record for ``bundle_path``, or ``None`` if none."""
    record = read_progress_items(bundle_path)
    if record is None:
        return None
    header, items = record
    return header, {usmp_id: item.result() for usmp_id, item in items.items()}


def read_progress_items(
    bundle_path: Path,
) -> tuple[ProgressHeader, dict[str, WrittenItem]] | None:
    """Like :func:`read_progress`, with everything recorded about each item."""
    path = progress_path(bundle_path)
    try:
        lines = path.read_bytes().splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProgressError(f"cannot read {path}: {exc}") from exc
    if not lines:
        raise ProgressError(f"{path} is empty")
    try:
        header = ProgressHeader.model_validate_json(lines[0])
    except ValidationError as exc:
        raise ProgressError(f"{path} is not a USMP progress record") from exc
    written: dict[str, WrittenItem] = {}
    for number, line in enumerate(lines[1:], start=2):
        try:
            item = WrittenItem.model_validate_json(line)
        except ValidationError as exc:
            if number == len(lines):
                break  # the run was stopped while writing this line
            raise ProgressError(f"{path} line {number} is damaged") from exc
        written[item.usmp_id] = item
    return header, written


class ProgressLog:
    """Appends written items to a progress record. Use as a context manager."""

    def __init__(
        self,
        bundle_path: Path,
        header: ProgressHeader,
        written: Mapping[str, WrittenItem] | None = None,
    ) -> None:
        """Start the record afresh with ``header`` and the items already ``written``."""
        self.path = progress_path(bundle_path)
        self.header = header
        self.items: dict[str, WrittenItem] = {}
        self._fh: IO[str]
        self._start(written or {})

    def _start(self, written: Mapping[str, WrittenItem]) -> None:
        """(Re)write the record, the header and then ``written``, replacing it atomically."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        with os.fdopen(os.open(tmp, flags, 0o600), "w", encoding="utf-8") as fh:
            fh.write(self.header.model_dump_json() + "\n")
            for item in written.values():
                fh.write(item.model_dump_json(exclude_defaults=True) + "\n")
        tmp.replace(self.path)
        self.items = dict(written)
        self._fh = self.path.open("a", encoding="utf-8")

    def mark_target_checked(self) -> None:
        """Record that the target was searched before writing (see ``target_checked``)."""
        if self.header.target_checked:
            return
        self.header = self.header.model_copy(update={"target_checked": True})
        self._fh.close()
        self._start(dict(self.items))

    @classmethod
    def start(
        cls,
        bundle_path: Path,
        *,
        items_sha256: str,
        domain: str,
        target: ConnectorInfo,
        target_connector: str,
        target_config: Mapping[str, str],
        selection: Selection | None = None,
        source_connector: str | None = None,
        source_config: Mapping[str, str] | None = None,
    ) -> Self:
        header = ProgressHeader(
            started_at=datetime.now(UTC),
            items_sha256=items_sha256,
            domain=domain,
            target=target,
            target_connector=target_connector,
            target_config=safe_config(target_config),
            selection=selection,
            source_connector=source_connector,
            source_config=safe_config(source_config) if source_config is not None else None,
        )
        return cls(bundle_path, header)

    def _line(self, text: str) -> None:
        self._fh.write(text + "\n")
        self._fh.flush()

    def _add(self, item: WrittenItem) -> None:
        self.items[item.usmp_id] = item
        self._line(item.model_dump_json(exclude_defaults=True))

    def record(self, result: WriteResult, *, already_present: bool | None) -> None:
        """Record an item now on the target: written by this run, or found already there."""
        self._add(
            WrittenItem(
                usmp_id=result.usmp_id,
                native_id=result.native_id,
                losses=list(result.losses),
                already_present=already_present,
                created=result.created,
            )
        )

    def verified(self, usmp_id: str, content_hash: str) -> None:
        """Record what a recorded item looked like when it was read back and verified."""
        item = self.items.get(usmp_id)
        if item is not None and item.verified_hash != content_hash:
            self._add(item.model_copy(update={"verified_hash": content_hash}))

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
