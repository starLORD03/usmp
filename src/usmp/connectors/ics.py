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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import icalendar

from usmp.connectors._ical import (
    HEX_COLOR,
    build_vcalendar,
    calendar_capabilities,
    event_records,
    parse_vcalendar,
    recurrence_key,
    resolve_zone,
    vevent,
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
             "X-APPLE-CALENDAR-COLOR", "X-WR-TIMEZONE"}  # fmt: skip
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
    events: list[Mapping[str, Any]] = field(default_factory=list)


class _IcsWriter(Writer):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._single = path.suffix.lower() == ".ics"

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

    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        files: dict[str, _PendingFile] = {}  # calendar usmp_id -> file
        native_ids: set[str] = set()
        default_key = "__default__"
        for item in items:
            if item.domain != "calendar" or item.type not in ("calendar", "event"):
                raise ConnectorError(f"cannot write {item.domain}/{item.type} items to iCalendar")
            if item.type == "calendar":
                path = self._new_file(files, str(item.payload["name"]))
                files[item.usmp_id] = _PendingFile(path, item.payload)
                yield WriteResult(usmp_id=item.usmp_id, native_id=path.name)
                continue
            key = item.parent if item.parent in files else default_key
            if key not in files:
                path = self._new_file(files, "imported")
                files[key] = _PendingFile(path, {"name": "Imported"})
            pending = files[key]
            rid = item.payload.get("recurrence_id")
            native_id = f"{pending.path.name}#{item.payload['uid']}" + (
                f"#{recurrence_key(vevent(item.payload))}" if rid else ""
            )
            if native_id in native_ids:
                raise ConnectorError(f"event {item.payload['uid']} appears more than once")
            native_ids.add(native_id)
            pending.events.append(item.payload)
            yield WriteResult(usmp_id=item.usmp_id, native_id=native_id)
        if not self._single:
            self._path.mkdir(parents=True, exist_ok=True)
        for pending in files.values():
            _write_file(pending)

    def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
        wanted = set(native_ids)
        files = {n.split("#", 1)[0] for n in wanted}
        base = self._path.parent if self._single else self._path
        for name in sorted(files):
            path = base / name
            if path.is_file():
                for record in _read_file(path):
                    if record.item.source.native_id in wanted:
                        yield record.item


def _write_file(pending: _PendingFile) -> None:
    meta = pending.calendar
    props = {"x-wr-calname": meta["name"]}
    for key, prop in (("description", "x-wr-caldesc"), ("color", "color"),
                      ("time_zone", "x-wr-timezone")):  # fmt: skip
        if meta.get(key):
            props[prop] = meta[key]
    data = build_vcalendar(pending.events, props)
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
