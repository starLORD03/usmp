"""iCalendar files (``.ics``), as exported by Google Calendar, Apple Calendar, Outlook and others.

``path`` is either one ``.ics`` file (one calendar) or a directory of ``.ics`` files (one calendar
per file). Every VEVENT becomes an ``event`` item. Properties the calendar model cannot hold are
reported as ``unsupported_by_canonical`` losses, and the original VEVENT is kept in the bundle.

Time zones: IANA names are kept; Windows names (``W. Europe Standard Time``) are mapped to IANA;
anything else is converted to UTC and reported as a ``value_converted`` loss.
"""

import hashlib
import os
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
from icalendar.timezone.windows_to_olson import WINDOWS_TO_OLSON
from pydantic import ValidationError

from usmp.core.models import (
    CapabilityManifest,
    DomainCapability,
    Item,
    LossReason,
    LossRecord,
    Severity,
    TypeCapability,
)
from usmp.domains.calendar import calendar_item, event_item
from usmp.domains.calendar.model import Calendar, Event
from usmp.sdk.connector import (
    Connector,
    ConnectorError,
    ExportRecord,
    Reader,
    Writer,
    WriteResult,
)

NAME = "ics"
_UTC_NAMES = frozenset({"UTC", "Etc/UTC", "GMT", "Etc/GMT", "Z", "Universal", "Zulu"})
_HANDLED = frozenset(
    {
        "UID", "DTSTART", "DTEND", "DURATION", "SUMMARY", "DESCRIPTION", "LOCATION", "URL",
        "STATUS", "TRANSP", "CLASS", "RRULE", "RDATE", "EXDATE", "RECURRENCE-ID", "ORGANIZER",
        "ATTENDEE", "CATEGORIES", "ATTACH", "COLOR",
    }
)  # fmt: skip
# Bookkeeping every target rewrites; deliberately outside the calendar model (spec §3).
_BOOKKEEPING = frozenset({"DTSTAMP", "CREATED", "LAST-MODIFIED", "SEQUENCE"})
_CALENDAR_IGNORED = frozenset({"PRODID", "VERSION", "CALSCALE", "METHOD"})
_ROLE_IN = {
    "CHAIR": "chair",
    "REQ-PARTICIPANT": "required",
    "OPT-PARTICIPANT": "optional",
    "NON-PARTICIPANT": "non_participant",
}
_ROLE_OUT = {v: k for k, v in _ROLE_IN.items()}
_PARTSTAT = {"NEEDS-ACTION", "ACCEPTED", "DECLINED", "TENTATIVE", "DELEGATED"}
_ATTENDEE_PARAMS_KEPT = frozenset({"CN", "ROLE", "PARTSTAT", "RSVP", "CUTYPE"})
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")

CALENDAR_FIELDS = sorted(Calendar.model_fields)
EVENT_FIELDS = sorted(Event.model_fields)


def _capabilities() -> CapabilityManifest:
    return CapabilityManifest(
        connector=NAME,
        display_name="iCalendar file",
        version="1",
        domains=[
            DomainCapability(
                domain="calendar",
                schema_version=1,
                types=[
                    TypeCapability(
                        type="calendar", read_fields=CALENDAR_FIELDS, write_fields=CALENDAR_FIELDS
                    ),
                    TypeCapability(
                        type="event", read_fields=EVENT_FIELDS, write_fields=EVENT_FIELDS
                    ),
                ],
            )
        ],
    )


# --- Reading -----------------------------------------------------------------------------------


def _resolve_zone(name: str | None) -> str | None:
    """IANA name for ``name``, or ``None`` if it cannot be resolved."""
    if not name:
        return None
    if name in _UTC_NAMES:
        return "UTC"
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return WINDOWS_TO_OLSON.get(name)


def _local(dt: datetime) -> str:
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def _ical_text(prop: Any) -> str:
    value = prop.to_ical()
    return value.decode() if isinstance(value, bytes) else str(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


@dataclass
class _Converter:
    """Converts one VEVENT, collecting losses as it goes."""

    native_id: str
    losses: list[tuple[str, LossReason, str]] = field(default_factory=list)

    def lose(
        self, name: str, detail: str, reason: LossReason = LossReason.UNSUPPORTED_BY_CANONICAL
    ) -> None:
        self.losses.append((name, reason, detail))

    def start(self, prop: Any) -> tuple[str, str | None, tzinfo | None]:
        """``(local value, IANA zone, tzinfo to convert other values into)``."""
        dt = prop.dt
        if not isinstance(dt, datetime):
            return dt.isoformat(), None, None
        if dt.tzinfo is None:
            return _local(dt), None, None
        tzid = prop.params.get("TZID")
        if tzid is None and dt.utcoffset() == timedelta(0):
            return _local(dt), "UTC", UTC
        zone = _resolve_zone(tzid or getattr(dt.tzinfo, "key", None) or str(dt.tzinfo))
        if zone is None:
            self.lose(
                "time_zone",
                f"unknown time zone {tzid!r}; times converted to UTC",
                LossReason.VALUE_CONVERTED,
            )
            return _local(dt.astimezone(UTC)), "UTC", UTC
        return _local(dt), zone, ZoneInfo(zone)

    def other(self, value: date | datetime, target: tzinfo | None) -> str:
        """Express ``value`` the same way as the event's start."""
        if not isinstance(value, datetime):
            return value.isoformat()
        if value.tzinfo is None or target is None:
            return _local(value)
        return _local(value.astimezone(target))


def _duration(td: timedelta) -> str:
    return str(icalendar.vDuration(td).to_ical().decode())  # type: ignore[no-untyped-call]


def _event_payload(ev: Any, conv: _Converter) -> dict[str, Any]:
    # icalendar property values are loosely typed unions; treat the component as dynamic.
    payload: dict[str, Any] = {}
    uid = str(ev.get("UID") or "")
    if not uid:
        uid = "usmp-" + hashlib.sha256(ev.to_ical()).hexdigest()[:24]
        conv.lose("uid", "event had no UID; one was generated", LossReason.VALUE_CONVERTED)
    payload["uid"] = uid

    if "DTSTART" not in ev:
        raise ConnectorError(f"event {uid} has no DTSTART")
    start, zone, tz = conv.start(ev["DTSTART"])
    payload["start"] = start
    if zone:
        payload["time_zone"] = zone
    all_day = "T" not in start

    if "DTEND" in ev:
        end_prop = ev["DTEND"]
        end_tzid = end_prop.params.get("TZID")
        end_zone = _resolve_zone(end_tzid) if end_tzid else None
        if end_zone and zone and end_zone != zone:
            payload["end"] = _local(end_prop.dt)
            payload["end_time_zone"] = end_zone
        else:
            payload["end"] = conv.other(end_prop.dt, tz)
    elif "DURATION" in ev:
        begin = ev["DTSTART"].dt
        end_dt = begin + ev["DURATION"].dt
        payload["end"] = (
            conv.other(end_dt, tz) if isinstance(end_dt, datetime) else end_dt.isoformat()
        )
    if payload.get("end") == payload["start"] and "end_time_zone" not in payload:
        del payload["end"]
        if all_day:
            conv.lose(
                "end", "zero-length all-day event treated as one day", LossReason.VALUE_CONVERTED
            )

    for prop, key in (("SUMMARY", "title"), ("DESCRIPTION", "description"),
                      ("LOCATION", "location"), ("URL", "url")):  # fmt: skip
        if prop in ev:
            payload[key] = str(ev[prop])

    if "STATUS" in ev:
        status = str(ev["STATUS"]).upper()
        if status in {"CONFIRMED", "TENTATIVE", "CANCELLED"}:
            payload["status"] = status.lower()
        else:
            conv.lose("status", f"status {status} is not supported")
    if "TRANSP" in ev:
        payload["availability"] = "free" if str(ev["TRANSP"]).upper() == "TRANSPARENT" else "busy"
    if "CLASS" in ev:
        klass = str(ev["CLASS"]).upper()
        if klass in {"PUBLIC", "PRIVATE", "CONFIDENTIAL"}:
            payload["privacy"] = klass.lower()
        else:
            conv.lose("privacy", f"class {klass} is not supported")

    payload["recurrence_rules"] = [r.to_ical().decode() for r in _as_list(ev.get("RRULE"))]
    for prop, key in (("RDATE", "recurrence_dates"), ("EXDATE", "excluded_dates")):
        values = []
        for group in _as_list(ev.get(prop)):
            for item in group.dts:
                if isinstance(item.dt, tuple):
                    conv.lose(key, "period values are not supported")
                    continue
                values.append(conv.other(item.dt, tz))
        payload[key] = values
    if "RECURRENCE-ID" in ev:
        payload["recurrence_id"] = conv.other(ev["RECURRENCE-ID"].dt, tz)

    if "ORGANIZER" in ev:
        org = ev["ORGANIZER"]
        email = str(org).removeprefix("mailto:").removeprefix("MAILTO:")
        if "@" in email:
            payload["organizer"] = {"email": email, "name": org.params.get("CN")}
        else:
            conv.lose("organizer", f"organizer {str(org)!r} has no email address")

    attendees: dict[str, dict[str, Any]] = {}
    for att in _as_list(ev.get("ATTENDEE")):
        email = str(att).removeprefix("mailto:").removeprefix("MAILTO:").lower()
        if "@" not in email:
            conv.lose("attendees", f"attendee {str(att)!r} has no email address")
            continue
        if email in attendees:
            conv.lose("attendees", f"duplicate attendee {email}", LossReason.VALUE_CONVERTED)
            continue
        entry: dict[str, Any] = {"email": email, "name": att.params.get("CN")}
        role = str(att.params.get("ROLE", "REQ-PARTICIPANT")).upper()
        entry["role"] = _ROLE_IN.get(role, "required")
        if role not in _ROLE_IN:
            conv.lose(
                "attendees", f"attendee role {role} treated as required", LossReason.VALUE_CONVERTED
            )
        partstat = str(att.params.get("PARTSTAT", "NEEDS-ACTION")).upper()
        if partstat in _PARTSTAT:
            entry["status"] = partstat.lower().replace("-", "_")
        else:
            conv.lose("attendees", f"attendee status {partstat} is not supported")
        cutype = str(att.params.get("CUTYPE", "INDIVIDUAL")).upper()
        extra = sorted(set(att.params) - _ATTENDEE_PARAMS_KEPT)
        if cutype != "INDIVIDUAL" or extra:
            details = ([f"CUTYPE={cutype}"] if cutype != "INDIVIDUAL" else []) + extra
            conv.lose("attendees", f"attendee {email}: {', '.join(details)} not kept")
        attendees[email] = entry
    payload["attendees"] = list(attendees.values())

    alarms = []
    for alarm in ev.walk("VALARM"):
        trigger = alarm.get("TRIGGER")
        action = str(alarm.get("ACTION", "DISPLAY")).upper()
        if trigger is None or not isinstance(trigger.dt, timedelta):
            conv.lose("alarms", "alarms at a fixed date and time are not supported")
            continue
        if str(trigger.params.get("RELATED", "START")).upper() != "START":
            conv.lose("alarms", "alarms relative to the end are not supported")
            continue
        if action not in {"DISPLAY", "EMAIL", "AUDIO"}:
            conv.lose("alarms", f"alarm action {action} is not supported")
            continue
        alarms.append({"trigger": _duration(trigger.dt), "action": action.lower()})
    payload["alarms"] = alarms

    payload["categories"] = [str(c) for group in _as_list(ev.get("CATEGORIES")) for c in group.cats]

    attachments = []
    for att in _as_list(ev.get("ATTACH")):
        if str(att.params.get("VALUE", "")).upper() == "BINARY" or att.params.get("ENCODING"):
            conv.lose("attachments", "embedded files are not copied")
            continue
        attachments.append(
            {
                "uri": str(att),
                "media_type": att.params.get("FMTTYPE"),
                "title": att.params.get("FILENAME") or att.params.get("X-FILENAME"),
            }
        )
    payload["attachments"] = attachments

    if "COLOR" in ev:
        color = str(ev["COLOR"])
        if _HEX_COLOR.fullmatch(color):
            payload["color"] = color[:7]
        else:
            conv.lose("color", f"colour {color!r} is not a hex colour")

    for name in ev:
        if name not in _HANDLED and name not in _BOOKKEEPING:
            conv.lose(name, f"iCalendar property {name} is not part of the calendar model")
    for sub in ev.subcomponents:
        if sub.name != "VALARM":
            conv.lose(str(sub.name), f"{sub.name} components are not part of the calendar model")
    return payload


def _calendar_payload(cal: icalendar.Calendar, path: Path) -> tuple[dict[str, Any], list[str]]:
    name = str(cal.get("X-WR-CALNAME") or cal.get("NAME") or path.stem)
    payload: dict[str, Any] = {"name": name}
    description = cal.get("X-WR-CALDESC") or cal.get("DESCRIPTION")
    if description:
        payload["description"] = str(description)
    color = str(cal.get("COLOR") or cal.get("X-APPLE-CALENDAR-COLOR") or "")
    if _HEX_COLOR.fullmatch(color):
        payload["color"] = color[:7]
    zone = _resolve_zone(str(cal.get("X-WR-TIMEZONE") or "")) if cal.get("X-WR-TIMEZONE") else None
    if zone:
        payload["time_zone"] = zone
    known = {"X-WR-CALNAME", "NAME", "X-WR-CALDESC", "DESCRIPTION", "COLOR",
             "X-APPLE-CALENDAR-COLOR", "X-WR-TIMEZONE"}  # fmt: skip
    unknown = sorted(set(cal) - known - _CALENDAR_IGNORED)
    return payload, unknown


def _parse(path: Path) -> icalendar.Calendar:
    try:
        return icalendar.Calendar.from_ical(path.read_bytes())
    except (ValueError, OSError) as exc:
        raise ConnectorError(f"cannot read {path}: {exc}") from exc


def _raw_name(native_id: str) -> str:
    return hashlib.sha256(native_id.encode()).hexdigest()[:32] + ".ics"


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

    seen: set[str] = set()
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID") or "")
        rid = ev.get("RECURRENCE-ID")
        native_id = f"{path.name}#{uid}" + (f"#{_ical_text(rid)}" if rid else "")
        if native_id in seen:
            raise ConnectorError(f"{path}: event {uid} appears more than once")
        seen.add(native_id)
        conv = _Converter(native_id)
        try:
            payload = _event_payload(ev, conv)
            item = event_item(NAME, native_id, payload, parent=cal_item.usmp_id)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise ConnectorError(
                f"{path}: event {uid or '(no UID)'} cannot be represented: "
                f"{'.'.join(map(str, first['loc']))}: {first['msg']}"
            ) from exc
        losses = [
            LossRecord(usmp_id=item.usmp_id, field=f, reason=r, stage="export", detail=d)
            for f, r, d in conv.losses
        ]
        yield ExportRecord(
            item=item, losses=losses, raw=ev.to_ical(), raw_name=_raw_name(native_id)
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


# --- Writing -----------------------------------------------------------------------------------


def _to_ical_time(value: str, zone: str | None) -> date | datetime:
    if "T" not in value:
        return date.fromisoformat(value)
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=ZoneInfo(zone)) if zone else dt


def _vevent(payload: Mapping[str, Any]) -> icalendar.Event:
    ev = icalendar.Event()
    zone = payload.get("time_zone")
    ev.add("uid", payload["uid"])
    ev.add("dtstamp", datetime.now(UTC))
    ev.add("dtstart", _to_ical_time(payload["start"], zone))
    if "end" in payload:
        ev.add("dtend", _to_ical_time(payload["end"], payload.get("end_time_zone", zone)))
    for key, prop in (("title", "summary"), ("description", "description"),
                      ("location", "location"), ("url", "url")):  # fmt: skip
        if key in payload:
            ev.add(prop, payload[key])
    if "status" in payload:
        ev.add("status", payload["status"].upper())
    if "availability" in payload:
        ev.add("transp", "TRANSPARENT" if payload["availability"] == "free" else "OPAQUE")
    if "privacy" in payload:
        ev.add("class", payload["privacy"].upper())
    for rule in payload.get("recurrence_rules", []):
        ev.add("rrule", icalendar.vRecur.from_ical(rule))
    if payload.get("recurrence_dates"):
        ev.add("rdate", [_to_ical_time(v, zone) for v in payload["recurrence_dates"]])
    if payload.get("excluded_dates"):
        ev.add("exdate", [_to_ical_time(v, zone) for v in payload["excluded_dates"]])
    if "recurrence_id" in payload:
        ev.add("recurrence-id", _to_ical_time(payload["recurrence_id"], zone))
    if "organizer" in payload:
        org = icalendar.vCalAddress("mailto:" + payload["organizer"]["email"])
        if payload["organizer"].get("name"):
            org.params["CN"] = payload["organizer"]["name"]
        ev.add("organizer", org)
    for att in payload.get("attendees", []):
        addr = icalendar.vCalAddress("mailto:" + att["email"])
        if att.get("name"):
            addr.params["CN"] = att["name"]
        addr.params["ROLE"] = _ROLE_OUT[att.get("role", "required")]
        addr.params["PARTSTAT"] = att.get("status", "needs_action").upper().replace("_", "-")
        ev.add("attendee", addr)
    for alarm in payload.get("alarms", []):
        valarm = icalendar.Alarm()
        valarm.add("action", alarm.get("action", "display").upper())
        trigger = icalendar.vDuration.from_ical(alarm["trigger"])  # type: ignore[no-untyped-call]
        valarm.add("trigger", trigger)
        valarm.add("description", payload.get("title") or "Reminder")
        ev.add_component(valarm)
    if payload.get("categories"):
        ev.add("categories", payload["categories"])
    for att in payload.get("attachments", []):
        params = {}
        if att.get("media_type"):
            params["FMTTYPE"] = att["media_type"]
        if att.get("title"):
            params["FILENAME"] = att["title"]
        ev.add("attach", att["uri"], parameters=params)
    if "color" in payload:
        ev.add("color", payload["color"])
    return ev


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
            ev = _vevent(item.payload)
            native_id = f"{pending.path.name}#{item.payload['uid']}" + (
                f"#{_ical_text(ev['RECURRENCE-ID'])}" if rid else ""
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
    cal = icalendar.Calendar()
    cal.add("prodid", "-//USMP//usmp-protocol//EN")
    cal.add("version", "2.0")
    meta = pending.calendar
    cal.add("x-wr-calname", meta["name"])
    if meta.get("description"):
        cal.add("x-wr-caldesc", meta["description"])
    if meta.get("color"):
        cal.add("color", meta["color"])
    if meta.get("time_zone"):
        cal.add("x-wr-timezone", meta["time_zone"])
    for payload in pending.events:
        cal.add_component(_vevent(payload))
    cal.add_missing_timezones()
    pending.path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pending.path.parent, prefix=".tmp-", suffix=".ics")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(cal.to_ical())
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
        return _capabilities()

    def open_reader(self) -> Reader:
        path = self.path
        if not path.exists():
            raise ConnectorError(f"{self.display_name}: {path} does not exist")
        return _IcsReader(path)

    def open_writer(self) -> Writer:
        return _IcsWriter(self.path)
