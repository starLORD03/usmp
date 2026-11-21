"""Reading and writing ``.usmp`` bundles (spec §7).

A bundle is a ZIP archive holding a snapshot of exported items. It doubles as a backup of the
source and as an offline transfer format. Bundles come from users, so the reader treats every
bundle as untrusted input: entry names, sizes and compression ratios are checked before anything
is decompressed, and every hash is verified.
"""

import json
import os
import re
import stat
import tempfile
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import TypeAdapter, ValidationError

from usmp import __version__
from usmp.core.hashing import canonical_json, sha256_hex
from usmp.core.models import (
    BundleManifest,
    CapabilityManifest,
    ConnectorInfo,
    Item,
    LossRecord,
    ToolInfo,
    TypeCount,
)

BUNDLE_SUFFIX = ".usmp"
MANIFEST = "manifest.json"
ITEMS = "items.jsonl"
LOSSES = "losses.json"
RAW_PREFIX = "raw/"
_RAW_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_LOSSES = TypeAdapter(list[LossRecord])

MiB = 1024 * 1024


class BundleError(Exception):
    """The bundle is unreadable, unsafe, or not a USMP bundle."""


@dataclass(frozen=True)
class Limits:
    """Safety limits applied when opening a bundle."""

    max_entries: int = 100_000
    max_entry_bytes: int = 1024 * MiB
    max_total_bytes: int = 4096 * MiB
    max_compression_ratio: int = 200
    """Only enforced for entries larger than ``ratio_threshold_bytes``."""
    ratio_threshold_bytes: int = 1 * MiB


def _check_raw_name(name: str) -> None:
    if not _RAW_NAME.fullmatch(name):
        raise BundleError(
            f"invalid raw entry name {name!r}: use letters, digits, '.', '_' or '-' "
            "(max 200 characters, not starting with a dot)"
        )


# --- Writer ------------------------------------------------------------------------------------


class BundleWriter:
    """Collect items, losses and raw payloads, then write a bundle atomically.

    Use as a context manager. Nothing is written to ``path`` unless the block completes without
    an exception, and an existing file is only replaced once the new bundle is complete.
    """

    def __init__(
        self,
        path: Path,
        *,
        source: ConnectorInfo,
        capabilities: CapabilityManifest,
    ) -> None:
        self.path = Path(path)
        self._source = source
        self._capabilities = capabilities
        self._items: dict[str, Item] = {}
        self._losses: list[LossRecord] = []
        self._raw: dict[str, bytes] = {}
        self.manifest: BundleManifest | None = None

    def add_item(self, item: Item) -> None:
        if item.usmp_id in self._items:
            raise BundleError(f"duplicate item {item.usmp_id}")
        self._items[item.usmp_id] = item

    def add_loss(self, loss: LossRecord) -> None:
        self._losses.append(loss)

    def add_raw(self, name: str, data: bytes) -> None:
        _check_raw_name(name)
        if name in self._raw:
            raise BundleError(f"duplicate raw entry {name!r}")
        self._raw[name] = data

    def close(self) -> BundleManifest:
        items = [self._items[k] for k in sorted(self._items)]
        items_bytes = b"".join(canonical_json(i.model_dump(mode="json")) + b"\n" for i in items)
        counts = Counter((i.domain, i.type) for i in items)
        manifest = BundleManifest(
            created_at=datetime.now(UTC),
            tool=ToolInfo(version=__version__),
            source=self._source,
            capabilities=self._capabilities,
            counts=[TypeCount(domain=d, type=t, count=n) for (d, t), n in sorted(counts.items())],
            items_sha256=sha256_hex(items_bytes),
            losses=len(self._losses),
            raw_entries=len(self._raw),
        )
        entries = [
            (MANIFEST, manifest.model_dump_json(indent=2).encode()),
            (ITEMS, items_bytes),
            (LOSSES, _LOSSES.dump_json(self._losses, indent=2)),
            *((RAW_PREFIX + name, self._raw[name]) for name in sorted(self._raw)),
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".usmp-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w") as zf:
                for name, data in entries:
                    info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    zf.writestr(info, data)
            Path(tmp).replace(self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.manifest = manifest
        return manifest

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.close()


# --- Reader ------------------------------------------------------------------------------------


class BundleReader:
    """Open a bundle safely and give access to its contents."""

    def __init__(self, path: Path, *, limits: Limits | None = None) -> None:
        self.path = Path(path)
        self.limits = limits or Limits()
        try:
            self._zip = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise BundleError(f"{self.path} is not a readable bundle: {exc}") from exc
        try:
            self._infos = self._check_entries()
            self.manifest = self._read_manifest()
        except BaseException:
            self._zip.close()
            raise

    # Structure checks happen before any entry is decompressed.
    def _check_entries(self) -> dict[str, zipfile.ZipInfo]:
        infos = self._zip.infolist()
        limits = self.limits
        if len(infos) > limits.max_entries:
            raise BundleError(f"too many entries ({len(infos)} > {limits.max_entries})")
        by_name: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for info in infos:
            name = info.filename
            if name in by_name:
                raise BundleError(f"duplicate entry {name!r}")
            if name.startswith(RAW_PREFIX):
                _check_raw_name(name.removeprefix(RAW_PREFIX))
            elif name not in (MANIFEST, ITEMS, LOSSES):
                raise BundleError(f"unexpected entry {name!r}")
            if info.flag_bits & 0x1:
                raise BundleError(f"encrypted entry {name!r} is not supported")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise BundleError(f"symbolic link entry {name!r} is not allowed")
            if info.file_size > limits.max_entry_bytes:
                raise BundleError(f"entry {name!r} is too large ({info.file_size} bytes)")
            ratio = info.file_size / max(info.compress_size, 1)
            if (
                info.file_size > limits.ratio_threshold_bytes
                and ratio > limits.max_compression_ratio
            ):
                raise BundleError(
                    f"entry {name!r} has a suspicious compression ratio ({ratio:.0f}:1)"
                )
            total += info.file_size
            if total > limits.max_total_bytes:
                raise BundleError("bundle is too large when uncompressed")
            by_name[name] = info
        for required in (MANIFEST, ITEMS, LOSSES):
            if required not in by_name:
                raise BundleError(f"missing required entry {required!r}")
        return by_name

    def _read(self, name: str) -> bytes:
        info = self._infos[name]
        try:
            with self._zip.open(info) as fh:
                # Never trust the size in the header: read at most one byte past it.
                data = fh.read(info.file_size + 1)
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise BundleError(f"entry {name!r} is corrupt: {exc}") from exc
        if len(data) != info.file_size:
            raise BundleError(f"entry {name!r} is corrupt: it does not match its declared size")
        return data

    def _read_manifest(self) -> BundleManifest:
        raw = self._read(MANIFEST)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BundleError(f"manifest.json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("format") != "usmp-bundle":
            raise BundleError("manifest.json does not describe a USMP bundle")
        if data.get("format_version") != 1:
            raise BundleError(
                f"unsupported bundle format_version {data.get('format_version')!r} "
                "(this version of usmp reads format_version 1)"
            )
        try:
            return BundleManifest.model_validate(data)
        except ValidationError as exc:
            raise BundleError(f"manifest.json is invalid: {exc}") from exc

    def items_bytes(self) -> bytes:
        return self._read(ITEMS)

    def iter_items(self) -> Iterator[Item]:
        """Yield items, raising :class:`BundleError` on the first invalid line."""
        for number, line in enumerate(self.items_bytes().splitlines(), start=1):
            try:
                yield Item.model_validate_json(line)
            except ValidationError as exc:
                raise BundleError(f"items.jsonl line {number} is invalid: {exc}") from exc

    def losses(self) -> list[LossRecord]:
        try:
            return _LOSSES.validate_json(self._read(LOSSES))
        except ValidationError as exc:
            raise BundleError(f"losses.json is invalid: {exc}") from exc

    @property
    def raw_names(self) -> list[str]:
        return sorted(n.removeprefix(RAW_PREFIX) for n in self._infos if n.startswith(RAW_PREFIX))

    def raw(self, name: str) -> bytes:
        try:
            return self._read(RAW_PREFIX + name)
        except KeyError:
            raise BundleError(f"no raw entry {name!r}") from None

    def validate(self) -> list[str]:
        """Check every integrity rule and return a list of problems (empty if the bundle is ok)."""
        problems: list[str] = []
        items_bytes = self.items_bytes()
        if sha256_hex(items_bytes) != self.manifest.items_sha256:
            problems.append("items.jsonl does not match items_sha256 in the manifest")
        items: list[Item] = []
        for number, line in enumerate(items_bytes.splitlines(), start=1):
            try:
                items.append(Item.model_validate_json(line))
            except ValidationError as exc:
                first = exc.errors()[0]["msg"]
                problems.append(f"items.jsonl line {number}: {first}")
        ids = [i.usmp_id for i in items]
        if len(ids) != len(set(ids)):
            problems.append("items.jsonl contains duplicate usmp_id values")
        if ids != sorted(ids):
            problems.append("items.jsonl is not sorted by usmp_id")
        actual = Counter((i.domain, i.type) for i in items)
        declared = {(c.domain, c.type): c.count for c in self.manifest.counts}
        if dict(actual) != {k: v for k, v in declared.items() if v}:
            problems.append(f"item counts {dict(actual)} do not match the manifest {declared}")
        try:
            losses = self.losses()
        except BundleError as exc:
            problems.append(str(exc))
        else:
            if len(losses) != self.manifest.losses:
                problems.append(
                    f"losses.json has {len(losses)} records, manifest says {self.manifest.losses}"
                )
            unknown = {loss.usmp_id for loss in losses} - set(ids)
            if unknown:
                problems.append(f"losses.json refers to {len(unknown)} item(s) not in the bundle")
        if len(self.raw_names) != self.manifest.raw_entries:
            problems.append(
                f"bundle has {len(self.raw_names)} raw entries, "
                f"manifest says {self.manifest.raw_entries}"
            )
        return problems

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "BUNDLE_SUFFIX",
    "BundleError",
    "BundleReader",
    "BundleWriter",
    "Limits",
]
