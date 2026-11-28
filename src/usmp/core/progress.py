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

from usmp.core.models import ConnectorInfo, LossRecord
from usmp.sdk.connector import WriteResult

PROGRESS_SUFFIX = ".progress.jsonl"
# Keys naming an environment variable (password_env) are fine; the value itself never is.
_SECRET_KEY = re.compile(r"pass|secret|token|credential|auth|key", re.IGNORECASE)


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


class WrittenItem(_Model):
    usmp_id: str
    native_id: str
    losses: list[LossRecord] = []

    def result(self) -> WriteResult:
        return WriteResult(self.usmp_id, self.native_id, tuple(self.losses))


def progress_path(bundle_path: Path) -> Path:
    return bundle_path.with_name(bundle_path.name + PROGRESS_SUFFIX)


def safe_config(config: Mapping[str, str]) -> dict[str, str]:
    """``config`` without anything that could be a secret."""
    return {k: v for k, v in config.items() if k.endswith("_env") or not _SECRET_KEY.search(k)}


def read_progress(bundle_path: Path) -> tuple[ProgressHeader, dict[str, WriteResult]] | None:
    """The header and written items of the record for ``bundle_path``, or ``None`` if none."""
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
    written: dict[str, WriteResult] = {}
    for number, line in enumerate(lines[1:], start=2):
        try:
            item = WrittenItem.model_validate_json(line)
        except ValidationError as exc:
            if number == len(lines):
                break  # the run was stopped while writing this line
            raise ProgressError(f"{path} line {number} is damaged") from exc
        written[item.usmp_id] = item.result()
    return header, written


class ProgressLog:
    """Appends written items to a progress record. Use as a context manager."""

    def __init__(
        self,
        bundle_path: Path,
        header: ProgressHeader,
        written: Mapping[str, WriteResult] | None = None,
    ) -> None:
        """Start the record afresh with ``header`` and the items already ``written``."""
        self.path = progress_path(bundle_path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        self._fh: IO[str] = os.fdopen(os.open(self.path, flags, 0o600), "w", encoding="utf-8")
        self._line(header.model_dump_json())
        for result in (written or {}).values():
            self.record(result)

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
    ) -> Self:
        header = ProgressHeader(
            started_at=datetime.now(UTC),
            items_sha256=items_sha256,
            domain=domain,
            target=target,
            target_connector=target_connector,
            target_config=safe_config(target_config),
        )
        return cls(bundle_path, header)

    def _line(self, text: str) -> None:
        self._fh.write(text + "\n")
        self._fh.flush()

    def record(self, result: WriteResult) -> None:
        item = WrittenItem(
            usmp_id=result.usmp_id, native_id=result.native_id, losses=list(result.losses)
        )
        self._line(item.model_dump_json())

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
