"""iCalendar conversion shared by the iCalendar-based connectors (``ics``, ``caldav``).

Converts VEVENT components to canonical ``usmp.calendar/v1`` event payloads (collecting losses)
and back. See ``spec/domains/calendar.md``.
"""

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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

USMP_ID = "X-USMP-ID"
"""Marks a VEVENT (or a VCALENDAR file) with the ``usmp_id`` it was written from, so re-runs can
recognise it. Not part of the calendar model: it identifies the item, it is not its content."""
USMP_ID_PATTERN = re.compile(r"^usmp:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
UTC_NAMES = frozenset({"UTC", "Etc/UTC", "GMT", "Etc/GMT", "Z", "Universal", "Zulu"})
HANDLED = frozenset(
    {
        "UID", "DTSTART", "DTEND", "DURATION", "SUMMARY", "DESCRIPTION", "LOCATION", "URL",
        "STATUS", "TRANSP", "CLASS", "RRULE", "RDATE", "EXDATE", "RECURRENCE-ID", "ORGANIZER",
        "ATTENDEE", "CATEGORIES", "ATTACH", "COLOR", "CONFERENCE", "X-USMP-PROVIDER-DATA",
        "X-GOOGLE-CONFERENCE", "X-MICROSOFT-SKYPETEAMSMEETINGURL", USMP_ID,
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
CUTYPES = frozenset({"INDIVIDUAL", "GROUP", "RESOURCE", "ROOM", "UNKNOWN"})
FEATURES = frozenset({"AUDIO", "CHAT", "FEED", "MODERATOR", "PHONE", "SCREEN", "VIDEO"})
# Meeting links that other apps keep in their own properties.
VENDOR_CONFERENCE = {
    "X-GOOGLE-CONFERENCE": ["video"],
    "X-MICROSOFT-SKYPETEAMSMEETINGURL": ["video"],
}
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


def conference_notes(conferences: Iterable[Mapping[str, Any]]) -> str:
    """Plain-text join details, added to an event's notes for apps that ignore CONFERENCE."""
    lines = []
    for conf in sorted(conferences, key=lambda c: ("video" not in c.get("features", []), c["uri"])):
        features = conf.get("features", [])
        if "video" in features:
            lines.append(f"Join online: {conf['uri']}")
        elif "phone" in features:
            lines.append(f"Join by phone: {conf.get('label') or conf['uri']}")
        else:
            lines.append(f"{conf.get('label') or 'Join'}: {conf['uri']}")
    return "\n".join(lines)


def notes_with_conferences(
    description: str | None, conferences: Sequence[Mapping[str, Any]]
) -> str | None:
    """``description`` plus join details for the conferences it does not already mention."""
    base = description or ""
    missing = [c for c in conferences if c["uri"] not in base]
    if not missing:
        return description
    block = conference_notes(missing)
    return f"{base}\n\n{block}" if base else block


def notes_without_conferences(
    description: str | None, conferences: Sequence[Mapping[str, Any]]
) -> str | None:
    """Undo :func:`notes_with_conferences`, so the notes read back exactly as they were."""
    if not description or not conferences:
        return description
    base, sep, _ = description.rpartition("\n\n")
    if sep and notes_with_conferences(base, conferences) == description:
        return base
    if notes_with_conferences(None, conferences) == description:
        return None
    return description


def _param_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else str(value).split(",")
    return [str(v).strip().upper() for v in values if str(v).strip()]


def _conference(prop: Any, conv: Converter) -> dict[str, Any]:
    features = _param_list(prop.params.get("FEATURE", ""))
    unknown = sorted(set(features) - FEATURES)
    if unknown:
        conv.lose(
            "conferences",
            f"conference features {', '.join(unknown)} are not supported",
            LossReason.VALUE_CONVERTED,
        )
    return {
        "uri": str(prop),
        # Trimmed here, as canonical form would, so the notes block matches (see _param).
        "label": (prop.params.get("LABEL") or "").strip() or None,
        "features": [f.lower() for f in features if f in FEATURES],
    }


def encode_provider_data(data: Mapping[str, Any]) -> str:
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(text.encode()).decode()


def decode_provider_data(value: str, conv: Converter) -> dict[str, Any]:
    try:
        data = json.loads(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        conv.lose("provider_data", "X-USMP-PROVIDER-DATA is damaged and was skipped")
        return {}
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        conv.lose("provider_data", "X-USMP-PROVIDER-DATA is not in the expected form")
        return {}
    return data


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
        end_dt = end_prop.dt
        if end_zone is None and isinstance(end_dt, datetime) and end_dt.tzinfo is not None:
            end_zone = "UTC" if end_dt.utcoffset() == timedelta(0) else None  # a "Z" time
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
    drop_backwards_end(payload, conv)

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
        if cutype in CUTYPES:
            entry["kind"] = cutype.lower()
        else:
            entry["kind"] = "unknown"
            conv.lose(
                "attendees",
                f"attendee {email}: CUTYPE={cutype} treated as unknown",
                LossReason.VALUE_CONVERTED,
            )
        extra = sorted(set(att.params) - ATTENDEE_PARAMS_KEPT)
        if extra:
            conv.lose("attendees", f"attendee {email}: {', '.join(extra)} not kept")
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

    conferences = [_conference(c, conv) for c in as_list(ev.get("CONFERENCE"))]
    known = {c["uri"] for c in conferences}
    for prop, features in VENDOR_CONFERENCE.items():
        for value in as_list(ev.get(prop)):
            if str(value) not in known:
                conferences.append({"uri": str(value), "features": features})
                known.add(str(value))
    payload["conferences"] = conferences
    if "description" in payload:
        notes = notes_without_conferences(payload["description"], conferences)
        if notes is None:
            del payload["description"]
        else:
            payload["description"] = notes

    if "X-USMP-PROVIDER-DATA" in ev:
        payload["provider_data"] = decode_provider_data(str(ev["X-USMP-PROVIDER-DATA"]), conv)

    for name in ev:
        if name not in HANDLED and name not in BOOKKEEPING:
            conv.lose(name, f"iCalendar property {name} is not part of the calendar model")
    for sub in ev.subcomponents:
        if sub.name != "VALARM":
            conv.lose(str(sub.name), f"{sub.name} components are not part of the calendar model")
    return payload


def raw_name(native_id: str) -> str:
    return hashlib.sha256(native_id.encode()).hexdigest()[:32] + ".ics"


def _param(value: str) -> str:
    """Parameter text that survives icalendar's parser.

    A value ending in a backslash makes icalendar treat the following ``:`` as escaped and
    corrupts the line. A trailing space forces quoting; canonical form trims it again on read.
    """
    return f"{value} " if value.endswith("\\") else value


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
    conferences = list(payload.get("conferences", []))
    notes = notes_with_conferences(payload.get("description"), conferences)
    texts = (("summary", payload.get("title")), ("description", notes),
             ("location", payload.get("location")), ("url", payload.get("url")))  # fmt: skip
    for prop, value in texts:
        if value is not None:
            ev.add(prop, value)
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
            org.params["CN"] = _param(payload["organizer"]["name"])
        ev.add("organizer", org)
    for att in payload.get("attendees", []):
        addr = icalendar.vCalAddress("mailto:" + att["email"])
        if att.get("name"):
            addr.params["CN"] = _param(att["name"])
        if att.get("kind", "individual") != "individual":
            addr.params["CUTYPE"] = att["kind"].upper()
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
            params["FMTTYPE"] = _param(att["media_type"])
        if att.get("title"):
            params["FILENAME"] = _param(att["title"])
        ev.add("attach", att["uri"], parameters=params)
    if "color" in payload:
        ev.add("color", payload["color"])
    for conf in conferences:
        params = {"VALUE": "URI"}
        if conf.get("features"):
            params["FEATURE"] = ",".join(f.upper() for f in conf["features"])
        if conf.get("label"):
            params["LABEL"] = _param(conf["label"])
        prop = icalendar.vUri(conf["uri"])
        prop_params: Any = prop.params
        prop_params.update(params)
        # Quote LABEL like CN, so a label ending in a backslash survives (see _param).
        prop_params.always_quoted = (*prop_params.always_quoted, "LABEL")
        ev.add("conference", prop)
    if payload.get("provider_data"):
        ev.add("x-usmp-provider-data", encode_provider_data(payload["provider_data"]))
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


def drop_backwards_end(payload: dict[str, Any], conv: Converter) -> None:
    """Move an event that ends before it starts without its end, and declare that.

    The calendar model needs ``end`` after ``start`` (spec ``calendar.md`` §3). Rather than stop
    the whole migration over one broken event, it is kept with no end (zero length, or one day if
    all-day); the original stays in the backup.
    """
    end = payload.get("end")
    if (
        end is not None
        and "end_time_zone" not in payload
        and len(str(end)) == len(str(payload["start"]))
        and str(end) < str(payload["start"])
    ):
        del payload["end"]
        conv.lose(
            "end",
            "it ended before it started, so it was moved without an end "
            "(the original is in the backup)",
            LossReason.VALUE_CONVERTED,
        )


def build_vcalendar(
    events: Iterable[Mapping[str, Any]],
    properties: Mapping[str, str] | None = None,
    *,
    timezones: bool = True,
    usmp_ids: Sequence[str] | None = None,
) -> bytes:
    """A complete VCALENDAR document with the given events and (by default) their time zones.

    ``usmp_ids``, one per event, marks each VEVENT with the item it was written from.
    """
    cal = icalendar.Calendar()
    cal.add("prodid", "-//USMP//usmp-protocol//EN")
    cal.add("version", "2.0")
    for name, value in (properties or {}).items():
        cal.add(name, value)
    events = list(events)
    if usmp_ids is not None and len(usmp_ids) != len(events):
        raise ValueError("give one usmp_id per event")
    for n, payload in enumerate(events):
        component = vevent(payload)
        if usmp_ids is not None:
            component.add(USMP_ID, usmp_ids[n])
        cal.add_component(component)
    if timezones:
        cal.add_missing_timezones()
    return bytes(cal.to_ical())


def marked_events(
    vcalendar: Any, native_id_for: Callable[[str, str | None], str]
) -> Iterator[tuple[str, str]]:
    """``(usmp_id, native_id)`` for every VEVENT that carries an X-USMP-ID marker."""
    for ev in vcalendar.walk("VEVENT"):
        marker = ev.get(USMP_ID)
        if marker is not None and USMP_ID_PATTERN.fullmatch(str(marker)):
            yield str(marker), native_id_for(str(ev.get("UID") or ""), recurrence_key(ev))


def parse_vcalendar(data: bytes | str, where: str) -> Any:
    try:
        return icalendar.Calendar.from_ical(data)
    except ValueError as exc:
        raise ConnectorError(f"cannot read {where}: {exc}") from exc


def unstorable_fields(payload: Mapping[str, Any]) -> list[str]:
    """Fields of ``payload`` that would not survive being written as iCalendar and read back.

    Writers report these as declared losses instead of letting verification find a mismatch.
    """
    vcal = parse_vcalendar(build_vcalendar([payload], timezones=False), "round-trip check")
    back = event_payload(vcal.walk("VEVENT")[0], Converter("check"))
    try:
        back = Event.model_validate(back).model_dump(mode="json", exclude_defaults=True)
    except ValidationError:
        return sorted(payload)
    return sorted(k for k in payload if back.get(k) != payload[k])
