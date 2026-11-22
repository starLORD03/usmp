"""iCalendar conversion shared by the iCalendar-based connectors (``ics``, ``caldav``).

Converts VEVENT components to canonical ``usmp.calendar/v1`` event payloads (collecting losses)
and back. See ``spec/domains/calendar.md``.
"""

import hashlib
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
from icalendar.timezone.windows_to_olson import WINDOWS_TO_OLSON
from pydantic import ValidationError

from usmp.core.models import (
    CapabilityManifest,
    DomainCapability,
    LossReason,
    LossRecord,
    TypeCapability,
)
from usmp.domains.calendar import event_item
from usmp.domains.calendar.model import Calendar, Event
from usmp.sdk.connector import ConnectorError, ExportRecord

UTC_NAMES = frozenset({"UTC", "Etc/UTC", "GMT", "Etc/GMT", "Z", "Universal", "Zulu"})
HANDLED = frozenset(
    {
        "UID", "DTSTART", "DTEND", "DURATION", "SUMMARY", "DESCRIPTION", "LOCATION", "URL",
        "STATUS", "TRANSP", "CLASS", "RRULE", "RDATE", "EXDATE", "RECURRENCE-ID", "ORGANIZER",
        "ATTENDEE", "CATEGORIES", "ATTACH", "COLOR",
    }
)  # fmt: skip
# Bookkeeping every target rewrites; deliberately outside the calendar model (spec §3).
BOOKKEEPING = frozenset({"DTSTAMP", "CREATED", "LAST-MODIFIED", "SEQUENCE"})
ROLE_IN = {
    "CHAIR": "chair",
    "REQ-PARTICIPANT": "required",
    "OPT-PARTICIPANT": "optional",
    "NON-PARTICIPANT": "non_participant",
}
ROLE_OUT = {v: k for k, v in ROLE_IN.items()}
PARTSTAT = {"NEEDS-ACTION", "ACCEPTED", "DECLINED", "TENTATIVE", "DELEGATED"}
ATTENDEE_PARAMS_KEPT = frozenset({"CN", "ROLE", "PARTSTAT", "RSVP", "CUTYPE"})
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")

CALENDAR_FIELDS = sorted(Calendar.model_fields)
EVENT_FIELDS = sorted(Event.model_fields)


def resolve_zone(name: str | None) -> str | None:
    """IANA name for ``name``, or ``None`` if it cannot be resolved."""
    if not name:
        return None
    if name in UTC_NAMES:
        return "UTC"
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return WINDOWS_TO_OLSON.get(name)


def local_time(dt: datetime) -> str:
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def ical_text(prop: Any) -> str:
    value = prop.to_ical()
    return value.decode() if isinstance(value, bytes) else str(value)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


@dataclass
class Converter:
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
            return local_time(dt), None, None
        tzid = prop.params.get("TZID")
        if tzid is None and dt.utcoffset() == timedelta(0):
            return local_time(dt), "UTC", UTC
        zone = resolve_zone(tzid or getattr(dt.tzinfo, "key", None) or str(dt.tzinfo))
        if zone is None:
            self.lose(
                "time_zone",
                f"unknown time zone {tzid!r}; times converted to UTC",
                LossReason.VALUE_CONVERTED,
            )
            return local_time(dt.astimezone(UTC)), "UTC", UTC
        return local_time(dt), zone, ZoneInfo(zone)

    def other(self, value: date | datetime, target: tzinfo | None) -> str:
        """Express ``value`` the same way as the event's start."""
        if not isinstance(value, datetime):
            return value.isoformat()
        if value.tzinfo is None or target is None:
            return local_time(value)
        return local_time(value.astimezone(target))


def duration(td: timedelta) -> str:
    return str(icalendar.vDuration(td).to_ical().decode())  # type: ignore[no-untyped-call]


def event_payload(ev: Any, conv: Converter) -> dict[str, Any]:
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
        end_zone = resolve_zone(end_tzid) if end_tzid else None
        if end_zone and zone and end_zone != zone:
            payload["end"] = local_time(end_prop.dt)
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

    payload["recurrence_rules"] = [r.to_ical().decode() for r in as_list(ev.get("RRULE"))]
    for prop, key in (("RDATE", "recurrence_dates"), ("EXDATE", "excluded_dates")):
        values = []
        for group in as_list(ev.get(prop)):
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
    for att in as_list(ev.get("ATTENDEE")):
        email = str(att).removeprefix("mailto:").removeprefix("MAILTO:").lower()
        if "@" not in email:
            conv.lose("attendees", f"attendee {str(att)!r} has no email address")
            continue
        if email in attendees:
            conv.lose("attendees", f"duplicate attendee {email}", LossReason.VALUE_CONVERTED)
            continue
        entry: dict[str, Any] = {"email": email, "name": att.params.get("CN")}
        role = str(att.params.get("ROLE", "REQ-PARTICIPANT")).upper()
        entry["role"] = ROLE_IN.get(role, "required")
        if role not in ROLE_IN:
            conv.lose(
                "attendees", f"attendee role {role} treated as required", LossReason.VALUE_CONVERTED
            )
        partstat = str(att.params.get("PARTSTAT", "NEEDS-ACTION")).upper()
        if partstat in PARTSTAT:
            entry["status"] = partstat.lower().replace("-", "_")
        else:
            conv.lose("attendees", f"attendee status {partstat} is not supported")
        cutype = str(att.params.get("CUTYPE", "INDIVIDUAL")).upper()
        extra = sorted(set(att.params) - ATTENDEE_PARAMS_KEPT)
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
        alarms.append({"trigger": duration(trigger.dt), "action": action.lower()})
    payload["alarms"] = alarms

    payload["categories"] = [str(c) for group in as_list(ev.get("CATEGORIES")) for c in group.cats]

    attachments = []
    for att in as_list(ev.get("ATTACH")):
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
        if HEX_COLOR.fullmatch(color):
            payload["color"] = color[:7]
        else:
            conv.lose("color", f"colour {color!r} is not a hex colour")

    for name in ev:
        if name not in HANDLED and name not in BOOKKEEPING:
            conv.lose(name, f"iCalendar property {name} is not part of the calendar model")
    for sub in ev.subcomponents:
        if sub.name != "VALARM":
            conv.lose(str(sub.name), f"{sub.name} components are not part of the calendar model")
    return payload


def raw_name(native_id: str) -> str:
    return hashlib.sha256(native_id.encode()).hexdigest()[:32] + ".ics"


def to_ical_time(value: str, zone: str | None) -> date | datetime:
    if "T" not in value:
        return date.fromisoformat(value)
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=ZoneInfo(zone)) if zone else dt


def vevent(payload: Mapping[str, Any]) -> icalendar.Event:
    ev = icalendar.Event()
    zone = payload.get("time_zone")
    ev.add("uid", payload["uid"])
    ev.add("dtstamp", datetime.now(UTC))
    ev.add("dtstart", to_ical_time(payload["start"], zone))
    if "end" in payload:
        ev.add("dtend", to_ical_time(payload["end"], payload.get("end_time_zone", zone)))
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
        ev.add("rdate", [to_ical_time(v, zone) for v in payload["recurrence_dates"]])
    if payload.get("excluded_dates"):
        ev.add("exdate", [to_ical_time(v, zone) for v in payload["excluded_dates"]])
    if "recurrence_id" in payload:
        ev.add("recurrence-id", to_ical_time(payload["recurrence_id"], zone))
    if "organizer" in payload:
        org = icalendar.vCalAddress("mailto:" + payload["organizer"]["email"])
        if payload["organizer"].get("name"):
            org.params["CN"] = payload["organizer"]["name"]
        ev.add("organizer", org)
    for att in payload.get("attendees", []):
        addr = icalendar.vCalAddress("mailto:" + att["email"])
        if att.get("name"):
            addr.params["CN"] = att["name"]
        addr.params["ROLE"] = ROLE_OUT[att.get("role", "required")]
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


def calendar_capabilities(connector: str, display_name: str) -> CapabilityManifest:
    """Every field of the calendar model, readable and writable."""
    return CapabilityManifest(
        connector=connector,
        display_name=display_name,
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


def recurrence_key(ev: Any) -> str | None:
    """The RECURRENCE-ID of a VEVENT as iCalendar text, or ``None`` for a master event."""
    rid = ev.get("RECURRENCE-ID")
    return ical_text(rid) if rid is not None else None


def event_records(
    vcalendar: Any,
    *,
    connector: str,
    parent: str | None,
    native_id_for: Callable[[str, str | None], str],
    where: str,
) -> Iterator[ExportRecord]:
    """Convert every VEVENT of ``vcalendar`` into an export record.

    ``native_id_for(uid, recurrence_id)`` names each event; ``where`` (a file or URL) is used in
    error messages.
    """
    seen: set[str] = set()
    for ev in vcalendar.walk("VEVENT"):
        uid = str(ev.get("UID") or "")
        native_id = native_id_for(uid, recurrence_key(ev))
        if native_id in seen:
            raise ConnectorError(f"{where}: event {uid} appears more than once")
        seen.add(native_id)
        conv = Converter(native_id)
        try:
            payload = event_payload(ev, conv)
            item = event_item(connector, native_id, payload, parent=parent)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise ConnectorError(
                f"{where}: event {uid or '(no UID)'} cannot be represented: "
                f"{'.'.join(map(str, first['loc']))}: {first['msg']}"
            ) from exc
        losses = [
            LossRecord(usmp_id=item.usmp_id, field=f, reason=r, stage="export", detail=d)
            for f, r, d in conv.losses
        ]
        yield ExportRecord(item=item, losses=losses, raw=ev.to_ical(), raw_name=raw_name(native_id))


def build_vcalendar(
    events: Iterable[Mapping[str, Any]], properties: Mapping[str, str] | None = None
) -> bytes:
    """A complete VCALENDAR document with the given events and their time zones."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//USMP//usmp-protocol//EN")
    cal.add("version", "2.0")
    for name, value in (properties or {}).items():
        cal.add(name, value)
    for payload in events:
        cal.add_component(vevent(payload))
    cal.add_missing_timezones()
    return bytes(cal.to_ical())


def parse_vcalendar(data: bytes | str, where: str) -> Any:
    try:
        return icalendar.Calendar.from_ical(data)
    except ValueError as exc:
        raise ConnectorError(f"cannot read {where}: {exc}") from exc
