"""Canonical items stored as JSON files in a directory.

Layout: ``<path>/<type>/<uuid>.json``, one item envelope per file. Because it stores canonical
items verbatim, this connector can read and write every field of every type, which makes it
useful as a local backup, for demos, and for testing other connectors.
"""

import os
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

from usmp.core.models import ANY, CapabilityManifest, DomainCapability, Item, TypeCapability
from usmp.sdk.connector import (
    Connector,
    ConnectorError,
    ExportRecord,
    Reader,
    Removal,
    RemovalOutcome,
    RemovalResult,
    Writer,
    WriteResult,
)

_NATIVE_ID = re.compile(
    r"^([a-z][a-z0-9_]*)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def _restamp(item: Item, native_id: str) -> Item:
    return Item.create(
        usmp_id=item.usmp_id,
        connector=JsonDirConnector.name,
        native_id=native_id,
        domain=item.domain,
        type=item.type,
        schema_version=item.schema_version,
        parent=item.parent,
        payload=item.payload,
        extensions=item.extensions,
    )


class JsonDirConnector(Connector):
    name = "jsondir"
    display_name = "JSON directory"
    description = "Canonical USMP items as JSON files in a local directory."
    config_keys: ClassVar[Mapping[str, str]] = {
        "path": "directory to read from or write to",
        "domain": "domain stored in the directory, e.g. calendar",
    }

    @property
    def path(self) -> Path:
        return Path(self.require("path")).expanduser()

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            connector=self.name,
            display_name=self.display_name,
            version="1",
            domains=[
                DomainCapability(
                    domain=self.require("domain"),
                    schema_version=1,
                    types=[TypeCapability(type=ANY, read_fields=[ANY], write_fields=[ANY])],
                )
            ],
        )

    def open_reader(self) -> Reader:
        if not self.path.is_dir():
            raise ConnectorError(f"{self.display_name}: {self.path} is not a directory")
        return _JsonDirReader(self.path)

    def open_writer(self) -> Writer:
        return _JsonDirWriter(self.path)


_OTHER_ITEM = "a different item is there"


def _load(path: Path, native_id: str) -> Item:
    try:
        return _restamp(Item.model_validate_json(path.read_bytes()), native_id)
    except (ValidationError, OSError) as exc:
        raise ConnectorError(f"cannot read item {path}: {exc}") from exc


class _JsonDirReader(Reader):
    def __init__(self, root: Path) -> None:
        self._root = root

    def read(self, domain: str) -> Iterator[ExportRecord]:
        items = []
        for file in sorted(self._root.glob("*/*.json")):
            native_id = f"{file.parent.name}/{file.stem}"
            if not _NATIVE_ID.fullmatch(native_id):
                continue
            item = _load(file, native_id)
            if item.domain == domain:
                items.append(item)
        # Parents before children, as the Reader contract requires.
        ids = {i.usmp_id: i for i in items}

        def depth(item: Item) -> int:
            d, parent = 0, item.parent
            while parent in ids and d < len(ids):
                d, parent = d + 1, ids[parent].parent
            return d

        for item in sorted(items, key=depth):
            yield ExportRecord(item=item)


def _native_id(item: Item) -> str:
    return f"{item.type}/{item.usmp_id.removeprefix('usmp:')}"


class _JsonDirWriter(Writer):
    def __init__(self, root: Path) -> None:
        self._root = root

    def already_written(
        self, items: Sequence[Item], known: Mapping[str, WriteResult]
    ) -> Iterator[WriteResult]:
        # Files are named after the usmp_id, so the target itself says what is there.
        for item in items:
            native_id = _native_id(item)
            if (self._root / f"{native_id}.json").is_file():
                yield WriteResult(usmp_id=item.usmp_id, native_id=native_id)

    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        for item in items:
            native_id = _native_id(item)
            target = self._root / f"{native_id}.json"
            if target.exists():
                raise ConnectorError(f"refusing to overwrite existing item {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            stored = _restamp(item, native_id)
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(stored.model_dump_json(indent=2) + "\n")
                Path(tmp).replace(target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            yield WriteResult(usmp_id=item.usmp_id, native_id=native_id)

    def remove(self, domain: str, removals: Sequence[Removal]) -> Iterator[RemovalResult]:
        for removal in removals:
            path = self._root / f"{removal.native_id}.json"
            if not _NATIVE_ID.fullmatch(removal.native_id) or not path.is_file():
                yield RemovalResult(removal.usmp_id, RemovalOutcome.ALREADY_GONE)
                continue
            item = _load(path, removal.native_id)
            if item.usmp_id != removal.usmp_id or item.domain != domain:
                yield RemovalResult(removal.usmp_id, RemovalOutcome.KEPT, _OTHER_ITEM)
            elif any(
                _load(other, f"{other.parent.name}/{other.stem}").parent == item.usmp_id
                for other in self._root.glob("*/*.json")
                if other != path and _NATIVE_ID.fullmatch(f"{other.parent.name}/{other.stem}")
            ):
                yield RemovalResult(removal.usmp_id, RemovalOutcome.KEPT, "it still holds items")
            else:
                path.unlink()
                yield RemovalResult(removal.usmp_id, RemovalOutcome.REMOVED)

    def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
        for native_id in native_ids:
            if not _NATIVE_ID.fullmatch(native_id):
                continue
            path = self._root / f"{native_id}.json"
            if path.is_file():
                item = _load(path, native_id)
                if item.domain == domain:
                    yield item
