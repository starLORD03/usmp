"""iCalendar files (``.ics``), as exported by Google Calendar, Apple Calendar, Outlook and others.

``path`` is either one ``.ics`` file (one calendar) or a directory of ``.ics`` files (one calendar
per file). Every VEVENT becomes an ``event`` item. Properties the calendar model cannot hold are
reported as ``unsupported_by_canonical`` losses, and the original VEVENT is kept in the bundle.

Time zones: IANA names are kept; Windows names (``W. Europe Standard Time``) are mapped to IANA;
anything else is converted to UTC and reported as a ``value_converted`` loss.
"""

import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import icalendar

from usmp.connectors._ical import (
    HEX_COLOR,
    NOT_EMPTY,
    NOT_MARKED,
    USMP_ID,
    USMP_ID_PATTERN,
    build_vcalendar,
    calendar_capabilities,
    event_records,
    holds_items,
    marked_events,
    parse_vcalendar,
    recurrence_key,
    removable_events,
    resolve_zone,
    unstorable_fields,
    vevent,
    without_events,
)
from usmp.core.models import (
    CapabilityManifest,
    Item,
    LossReason,
    LossRecord,
    Severity,
)
from usmp.domains.calendar import calendar_item
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

NAME = "ics"

_CALENDAR_IGNORED = frozenset({"PRODID", "VERSION", "CALSCALE", "METHOD"})


def _calendar_payload(cal: icalendar.Calendar, path: Path) -> tuple[dict[str, Any], list[str]]:
    name = str(cal.get("X-WR-CALNAME") or cal.get("NAME") or path.stem)
    payload: dict[str, Any] = {"name": name}
    description = cal.get("X-WR-CALDESC") or cal.get("DESCRIPTION")
    if description:
        payload["description"] = str(description)
    color = str(cal.get("COLOR") or cal.get("X-APPLE-CALENDAR-COLOR") or "")
    if HEX_COLOR.fullmatch(color):
        payload["color"] = color[:7]
    zone = resolve_zone(str(cal.get("X-WR-TIMEZONE") or "")) if cal.get("X-WR-TIMEZONE") else None
    if zone:
        payload["time_zone"] = zone
    known = {"X-WR-CALNAME", "NAME", "X-WR-CALDESC", "DESCRIPTION", "COLOR",
             "X-APPLE-CALENDAR-COLOR", "X-WR-TIMEZONE", USMP_ID}  # fmt: skip
    unknown = sorted(set(cal) - known - _CALENDAR_IGNORED)
    return payload, unknown


def _parse(path: Path) -> Any:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConnectorError(f"cannot read {path}: {exc}") from exc
    return parse_vcalendar(data, str(path))


def _read_file(path: Path) -> Iterator[ExportRecord]:
    cal = _parse(path)
    cal_payload, unknown = _calendar_payload(cal, path)
    cal_item = calendar_item(NAME, path.name, cal_payload)
    cal_losses = [
        LossRecord(usmp_id=cal_item.usmp_id, field=p, reason=LossReason.UNSUPPORTED_BY_CANONICAL,
                   stage="export", detail=f"calendar property {p} is not part of the model")
        for p in unknown
    ]  # fmt: skip
    skipped = [c.name for c in cal.subcomponents if c.name in {"VTODO", "VJOURNAL", "VFREEBUSY"}]
    for kind in sorted(set(skipped)):
        cal_losses.append(
            LossRecord(
                usmp_id=cal_item.usmp_id,
                field=kind,
                reason=LossReason.UNSUPPORTED_BY_CANONICAL,
                severity=Severity.ERROR,
                stage="export",
                detail=f"{skipped.count(kind)} {kind} component(s) are not calendar events "
                "and were not exported",
            )
        )
    yield ExportRecord(item=cal_item, losses=cal_losses)

    def native_id(uid: str, rid: str | None) -> str:
        return f"{path.name}#{uid}" + (f"#{rid}" if rid else "")

    yield from event_records(
        cal, connector=NAME, parent=cal_item.usmp_id, native_id_for=native_id, where=str(path)
    )


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.suffix.lower() == ".ics" and p.is_file())
    raise ConnectorError(f"{path} does not exist")


class _IcsReader(Reader):
    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, domain: str) -> Iterator[ExportRecord]:
        if domain != "calendar":
            return
        for file in _files(self._path):
            yield from _read_file(file)


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.").lower()
    return slug or "calendar"


@dataclass
class _PendingFile:
    path: Path
    calendar: Mapping[str, Any]
    usmp_id: str | None = None
    events: list[Mapping[str, Any]] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)


def _event_native_id(file_name: str, payload: Mapping[str, Any]) -> str:
    rid = payload.get("recurrence_id")
    return f"{file_name}#{payload['uid']}" + (f"#{recurrence_key(vevent(payload))}" if rid else "")


def _event_losses(item: Item) -> tuple[LossRecord, ...]:
    return tuple(
        LossRecord(usmp_id=item.usmp_id, field=f, reason=LossReason.VALUE_CONVERTED,
                   stage="import", detail="iCalendar cannot store this value exactly")
        for f in unstorable_fields(item.payload)
    )  # fmt: skip


class _IcsWriter(Writer):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._single = path.suffix.lower() == ".ics"
        self._present: dict[str, Path] = {}  # calendars an earlier run wrote

    def _new_file(self, files: dict[str, _PendingFile], name: str) -> Path:
        if self._single:
            if files:
                raise ConnectorError(
                    f"{self._path} is a single file but the data has several calendars; "
                    "give a directory instead"
                )
            target = self._path
        else:
            stem, n = _slug(name), 1
            target = self._path / f"{stem}.ics"
            taken = {f.path for f in files.values()}
            while target in taken or target.exists():
                n += 1
                target = self._path / f"{stem}-{n}.ics"
        if target.exists():
            raise ConnectorError(f"refusing to overwrite existing file {target}")
        return target

    def _existing_files(self) -> list[Path]:
        if self._single:
            return [self._path] if self._path.is_file() else []
        if not self._path.is_dir():
            return []
        return sorted(p for p in self._path.iterdir() if p.suffix.lower() == ".ics" and p.is_file())

    def already_written(
        self, items: Sequence[Item], known: Mapping[str, WriteResult]
    ) -> Iterator[WriteResult]:
        # Files are written whole at the end, so the files themselves are the only truth.
        files: dict[str, Path] = {}  # calendar usmp_id -> file
        events: dict[str, str] = {}  # event usmp_id -> native id
        for path in self._existing_files():
            cal = _parse(path)
            marker = str(cal.get(USMP_ID) or "")
            if USMP_ID_PATTERN.fullmatch(marker):
                files[marker] = path

            def native_id(uid: str, rid: str | None, name: str = path.name) -> str:
                return f"{name}#{uid}" + (f"#{rid}" if rid else "")

            events.update(marked_events(cal, native_id))
        for item in items:
            if item.type == "calendar" and item.usmp_id in files:
                self._present[item.usmp_id] = files[item.usmp_id]
                yield WriteResult(usmp_id=item.usmp_id, native_id=files[item.usmp_id].name)
            elif item.type == "event" and item.usmp_id in events:
                yield WriteResult(item.usmp_id, events[item.usmp_id], _event_losses(item))

    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        files: dict[str, _PendingFile] = {}  # calendar usmp_id -> file
        results: list[WriteResult] = []
        native_ids: set[str] = set()
        default_key = "__default__"
        for item in items:
            if item.domain != "calendar" or item.type not in ("calendar", "event"):
                raise ConnectorError(f"cannot write {item.domain}/{item.type} items to iCalendar")
            if item.type == "calendar":
                path = self._new_file(files, str(item.payload["name"]))
                files[item.usmp_id] = _PendingFile(path, item.payload, item.usmp_id)
                results.append(WriteResult(item.usmp_id, path.name, created=True))
                continue
            if item.parent in self._present:
                raise ConnectorError(
                    f"{self._present[item.parent]} already holds this calendar from an earlier "
                    f"run; refusing to overwrite it to add event {item.payload['uid']}"
                )
            key = item.parent if item.parent in files else default_key
            if key not in files:
                path = self._new_file(files, "imported")
                files[key] = _PendingFile(path, {"name": "Imported"})
            pending = files[key]
            native_id = _event_native_id(pending.path.name, item.payload)
            if native_id in native_ids:
                raise ConnectorError(f"event {item.payload['uid']} appears more than once")
            native_ids.add(native_id)
            pending.events.append(item.payload)
            pending.event_ids.append(item.usmp_id)
            results.append(WriteResult(item.usmp_id, native_id, _event_losses(item)))
        if not self._single:
            self._path.mkdir(parents=True, exist_ok=True)
        for pending in files.values():
            _write_file(pending)
        # Only report items once their file is safely on disk.
        yield from results

    @property
    def _base(self) -> Path:
        return self._path.parent if self._single else self._path

    def remove(self, domain: str, removals: Sequence[Removal]) -> Iterator[RemovalResult]:
        if domain != "calendar":
            return
        events: dict[str, list[Removal]] = defaultdict(list)
        for removal in removals:
            if removal.type == "event":
                events[removal.native_id.split("#", 1)[0]].append(removal)
        for name, group in sorted(events.items()):
            yield from self._remove_events(self._base / name, group)
        for removal in removals:
            if removal.type == "calendar":
                yield self._remove_file(removal)
            elif removal.type != "event":
                yield RemovalResult(removal.usmp_id, RemovalOutcome.KEPT, "not a calendar item")

    def _remove_events(self, path: Path, group: Sequence[Removal]) -> Iterator[RemovalResult]:
        if not path.is_file():
            for removal in group:
                yield RemovalResult(removal.usmp_id, RemovalOutcome.ALREADY_GONE)
            return
        original = path.read_bytes()
        cal = parse_vcalendar(original, str(path))

        def native_id(uid: str, rid: str | None) -> str:
            return f"{path.name}#{uid}" + (f"#{rid}" if rid else "")

        drop, outcomes = removable_events(cal, group, native_id)
        if drop:
            _replace(path, original, without_events(cal, drop).to_ical())
        yield from outcomes.values()

    def _remove_file(self, removal: Removal) -> RemovalResult:
        path = self._base / removal.native_id
        if not path.is_file():
            return RemovalResult(removal.usmp_id, RemovalOutcome.ALREADY_GONE)
        data = path.read_bytes()
        cal = parse_vcalendar(data, str(path))
        if str(cal.get(USMP_ID) or "") != removal.usmp_id:
            return RemovalResult(removal.usmp_id, RemovalOutcome.KEPT, NOT_MARKED)
        if holds_items(cal):
            return RemovalResult(removal.usmp_id, RemovalOutcome.KEPT, NOT_EMPTY)
        if path.read_bytes() != data:
            raise ConnectorError(f"{path} changed while undoing; nothing more was removed")
        path.unlink()
        return RemovalResult(removal.usmp_id, RemovalOutcome.REMOVED)

    def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
        wanted = set(native_ids)
        files = {n.split("#", 1)[0] for n in wanted}
        base = self._base
        for name in sorted(files):
            path = base / name
            if path.is_file():
                for record in _read_file(path):
                    if record.item.source.native_id in wanted:
                        yield record.item


def _replace(path: Path, expected: bytes, data: bytes) -> None:
    """Atomically replace ``path`` with ``data``, unless it no longer holds ``expected``."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".ics")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if path.read_bytes() != expected:
            raise ConnectorError(f"{path} changed while undoing; nothing more was removed")
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_file(pending: _PendingFile) -> None:
    meta = pending.calendar
    props = {"x-wr-calname": meta["name"]}
    if pending.usmp_id:
        props[USMP_ID.lower()] = pending.usmp_id
    for key, prop in (("description", "x-wr-caldesc"), ("color", "color"),
                      ("time_zone", "x-wr-timezone")):  # fmt: skip
        if meta.get(key):
            props[prop] = meta[key]
    data = build_vcalendar(pending.events, props, usmp_ids=pending.event_ids)
    pending.path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pending.path.parent, prefix=".tmp-", suffix=".ics")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if pending.path.exists():
            raise ConnectorError(f"refusing to overwrite existing file {pending.path}")
        Path(tmp).replace(pending.path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- Connector ---------------------------------------------------------------------------------


class IcsConnector(Connector):
    name = NAME
    display_name = "iCalendar file"
    description = "Calendars as .ics files: exports from Google, Apple, Outlook, Thunderbird."
    config_keys: ClassVar[Mapping[str, str]] = {
        "path": "an .ics file, or a directory with one .ics file per calendar",
    }

    @property
    def path(self) -> Path:
        return Path(self.require("path")).expanduser()

    def capabilities(self) -> CapabilityManifest:
        return calendar_capabilities(NAME, "iCalendar file")

    def open_reader(self) -> Reader:
        path = self.path
        if not path.exists():
            raise ConnectorError(f"{self.display_name}: {path} does not exist")
        return _IcsReader(path)

    def open_writer(self) -> Writer:
        return _IcsWriter(self.path)
