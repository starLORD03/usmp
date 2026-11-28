"""Google Calendar, through the Calendar API v3.

Install with ``pip install usmp-protocol[google]``. Sign in once with your own OAuth client::

    usmp auth google --client-secrets client_secret.json            # read-only, for sources
    usmp auth google --client-secrets client_secret.json --write    # also needed for targets

Then use ``google:account=you@gmail.com``. Tokens live in the OS keychain.

As a source, requests go through a session that refuses everything except GET. As a target,
events are written with ``events.import``, which keeps each event's iCalendar UID and organizer
and sends no invitations; modified occurrences update the matching instance of the series.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, NoReturn
from urllib.parse import quote
from zoneinfo import ZoneInfo

from usmp.connectors._ical import (
    Converter,
    event_payload,
    local_time,
    notes_with_conferences,
    parse_vcalendar,
    resolve_zone,
)
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
from usmp.sdk import credentials
from usmp.sdk.connector import (
    Connector,
    ConnectorError,
    ExportRecord,
    Reader,
    Writer,
    WriteResult,
)
from usmp.sdk.retry import RetryPolicy, TransientError, parse_retry_after, retrying

NAME = "google"
API = "https://www.googleapis.com/calendar/v3"
READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar"
# 429 and a rate-limit 403 mean the request was not carried out; 5xx answers may hide a write.
_NOT_DONE_STATUS = frozenset({429})
_UNCLEAR_STATUS = frozenset({500, 502, 503, 504})
# Repeating these cannot change the outcome. POST is only repeated when the caller can find its
# own earlier write (``recover``) or the server answered that nothing was done.
_REPEATABLE = frozenset({"GET", "PUT", "PATCH", "DELETE"})

CALENDAR_READ = ["color", "description", "name", "time_zone"]
CALENDAR_WRITE = CALENDAR_READ
EVENT_READ = sorted(
    {
        "uid", "title", "description", "location", "start", "end", "time_zone", "end_time_zone",
        "status", "availability", "privacy", "recurrence_rules", "recurrence_dates",
        "excluded_dates", "recurrence_id", "organizer", "attendees", "alarms", "attachments",
        "conferences", "provider_data",
    }
)  # fmt: skip
# Google only stores Drive attachments, and alarms must be before the start (checked per item).
EVENT_WRITE = sorted(set(EVENT_READ) - {"attachments"})
_ROLE_LOSS = frozenset({"chair", "non_participant"})
_STATUS_OUT = {"needs_action": "needsAction", "delegated": "needsAction"}
# Event properties mapped to the calendar model, or bookkeeping that Google rewrites anyway.
# Everything else is kept under provider_data["google"] so it can be restored on Google.
_MAPPED = frozenset(
    {
        "kind", "etag", "id", "htmlLink", "created", "updated", "creator", "iCalUID", "sequence",
        "summary", "description", "location", "start", "end", "status", "transparency",
        "visibility", "recurrence", "recurringEventId", "originalStartTime", "organizer",
        "attendees", "reminders", "attachments", "endTimeUnspecified", "hangoutLink",
    }
)  # fmt: skip
# provider_data["google"] properties that events.import accepts.
_RESTORABLE = frozenset(
    {
        "colorId", "conferenceData", "extendedProperties", "source", "guestsCanInviteOthers",
        "guestsCanModify", "guestsCanSeeOtherGuests", "anyoneCanAddSelf", "privateCopy",
    }
)  # fmt: skip
_CONF = {"conferenceDataVersion": 1}  # keep conferenceData when writing
_USMP_KEY = "usmp_id"  # extendedProperties.private key marking events USMP wrote
_ENTRY_FEATURES = {"video": ["video"], "phone": ["phone"], "sip": ["audio"]}


def capabilities() -> CapabilityManifest:
    return CapabilityManifest(
        connector=NAME,
        display_name="Google Calendar",
        version="1",
        domains=[
            DomainCapability(
                domain="calendar",
                schema_version=1,
                types=[
                    TypeCapability(
                        type="calendar", read_fields=CALENDAR_READ, write_fields=CALENDAR_WRITE
                    ),
                    TypeCapability(type="event", read_fields=EVENT_READ, write_fields=EVENT_WRITE),
                ],
            )
        ],
    )


def _zone(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _require_google() -> Any:
    try:
        import google.auth.transport.requests as transport
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ConnectorError(
            "Google support is not installed: pip install 'usmp-protocol[google]'"
        ) from exc
    return transport


# --- HTTP --------------------------------------------------------------------------------------


class _Api:
    """Thin client for the Calendar API with retries and plain-language errors."""

    policy: ClassVar[RetryPolicy | None] = None  # None: the SDK default

    def __init__(self, session: Any, *, read_only: bool) -> None:
        self._session = session
        self._read_only = read_only

    def call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        recover: Callable[[], Any] | None = None,
    ) -> Any:
        """Send one request, retrying rate limits, busy servers and dropped connections.

        ``recover`` looks for the result of an earlier try of a create whose answer never
        arrived. Without it, a create is only retried when Google said it did nothing.
        """
        if self._read_only and method != "GET":
            raise ConnectorError(f"blocked a {method} request: this source is read-only")
        url = API + path
        repeatable = method in _REPEATABLE
        unclear: list[bool] = []  # an earlier try may have been carried out

        def attempt() -> Any:
            try:
                response = self._session.request(method, url, params=params, json=body, timeout=60)
            except OSError as exc:
                return self._unclear(f"cannot reach Google Calendar: {exc}", None, unclear,
                                     repeatable or recover is not None)  # fmt: skip
            status = response.status_code
            wait = parse_retry_after(response.headers.get("Retry-After"))
            text = response.text
            if status in _NOT_DONE_STATUS or (
                status == 403 and "ratelimitexceeded" in text.lower()
            ):
                raise TransientError(f"Google Calendar is rate limiting (HTTP {status})",
                                     retry_after=wait)  # fmt: skip
            if status in _UNCLEAR_STATUS:
                return self._unclear(f"Google Calendar error {status}", wait, unclear,
                                     repeatable or recover is not None, text)  # fmt: skip
            if status == 409 and unclear and recover is not None:
                found = recover()
                if found is not None:
                    return found
            return self._answer(status, text, response)

        return retrying(
            attempt,
            what="Google Calendar",
            policy=self.policy,
            before_retry=(lambda: recover() if unclear else None) if recover else None,
        )

    @staticmethod
    def _unclear(
        message: str, wait: float | None, unclear: list[bool], retry: bool, text: str = ""
    ) -> NoReturn:
        if not retry:
            detail = f": {text[:300]}" if text else ""
            raise ConnectorError(f"{message}{detail} (not retried: it might have been saved)")
        unclear.append(True)
        raise TransientError(message, retry_after=wait)

    @staticmethod
    def _answer(status: int, text: str, response: Any) -> Any:
        if status == 401:
            raise ConnectorError(
                "Google sign-in expired or was revoked: run `usmp auth google` again"
            )
        if status == 403 and "insufficient" in text.lower():
            raise ConnectorError(
                "this Google sign-in is read-only: run `usmp auth google --write` to allow writing"
            )
        if status == 409:
            raise ConnectorError("the event already exists in the target calendar")
        if status >= 400:
            raise ConnectorError(f"Google Calendar error {status}: {text[:300]}")
        return response.json() if response.content else None

    def pages(self, path: str, params: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
        token: str | None = None
        while True:
            page = self.call(
                "GET", path, params={**params, **({"pageToken": token} if token else {})}
            )
            yield from page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return


# --- Google event -> canonical -----------------------------------------------------------------


def _to_local(when: Mapping[str, Any], zone: str | None) -> tuple[str, str | None]:
    """``(local value, zone)`` for a Google ``start``/``end`` object."""
    if "date" in when:
        return str(when["date"]), None
    dt = datetime.fromisoformat(str(when["dateTime"]).replace("Z", "+00:00"))
    name = resolve_zone(when.get("timeZone")) or zone or "UTC"
    return local_time(dt.astimezone(ZoneInfo(name))), name


def _ical_dtstart(start: str, zone: str | None) -> str:
    if "T" not in start:
        return f"DTSTART;VALUE=DATE:{start.replace('-', '')}"
    value = start.replace("-", "").replace(":", "")
    return f"DTSTART;TZID={zone}:{value}" if zone else f"DTSTART:{value}"


def _recurrence(
    lines: Sequence[str], start: str, zone: str | None, conv: Converter
) -> dict[str, Any]:
    """Parse Google's RRULE/RDATE/EXDATE lines with the shared iCalendar converter."""
    text = "\r\n".join(
        ["BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:x", _ical_dtstart(start, zone), *lines,
         "END:VEVENT", "END:VCALENDAR"]
    )  # fmt: skip
    vevent = parse_vcalendar(text, "Google recurrence").walk("VEVENT")[0]
    payload = event_payload(vevent, conv)
    return {k: payload[k] for k in ("recurrence_rules", "recurrence_dates", "excluded_dates")}


class _EventConverter:
    def __init__(
        self, calendar_zone: str | None, default_reminders: Sequence[Mapping[str, Any]] = ()
    ) -> None:
        self.zone = calendar_zone
        self.default_reminders = list(default_reminders)

    def convert(self, ev: Mapping[str, Any], conv: Converter) -> dict[str, Any]:
        payload: dict[str, Any] = {"uid": ev.get("iCalUID") or ev["id"]}
        start, zone = _to_local(ev["start"], self.zone)
        payload["start"] = start
        if zone:
            payload["time_zone"] = zone
        if "end" in ev and not ev.get("endTimeUnspecified"):
            end, end_zone = _to_local(ev["end"], zone)
            split = bool(end_zone and zone and end_zone != zone)
            if end != start or split:  # a zero-length event has no end in the calendar model
                payload["end"] = end
                if split:
                    payload["end_time_zone"] = end_zone
        for key, name in (("summary", "title"), ("description", "description"),
                          ("location", "location")):  # fmt: skip
            if ev.get(key):
                payload[name] = ev[key]
        payload["status"] = ev.get("status", "confirmed")
        payload["availability"] = "free" if ev.get("transparency") == "transparent" else "busy"
        if ev.get("visibility") in ("public", "private", "confidential"):
            payload["privacy"] = ev["visibility"]
        if ev.get("recurrence"):
            payload.update(_recurrence(ev["recurrence"], start, zone, conv))
        if "originalStartTime" in ev:
            payload["recurrence_id"], _ = _to_local(ev["originalStartTime"], zone)
        organizer = ev.get("organizer") or {}
        if organizer.get("email"):
            payload["organizer"] = {
                "email": organizer["email"],
                "name": organizer.get("displayName"),
            }
        attendees = []
        for att in ev.get("attendees", []):
            if not att.get("email"):
                conv.lose("attendees", "an attendee without an email address")
                continue
            attendees.append(
                {
                    "email": att["email"],
                    "kind": "resource" if att.get("resource") else "individual",
                    "name": att.get("displayName"),
                    "role": "optional" if att.get("optional") else "required",
                    "status": {"needsAction": "needs_action", "accepted": "accepted",
                               "declined": "declined", "tentative": "tentative"}.get(
                        att.get("responseStatus", "needsAction"), "needs_action"),
                }
            )  # fmt: skip
        payload["attendees"] = attendees
        reminders = ev.get("reminders") or {}
        # "Use default reminders" means the calendar's defaults: copy them onto the event.
        chosen = (
            self.default_reminders
            if reminders.get("useDefault")
            else reminders.get("overrides", [])
        )
        payload["alarms"] = [
            {"trigger": f"-PT{int(r['minutes'])}M",
             "action": "email" if r.get("method") == "email" else "display"}
            for r in chosen
        ]  # fmt: skip
        payload["attachments"] = [
            {"uri": a["fileUrl"], "title": a.get("title"), "media_type": a.get("mimeType")}
            for a in ev.get("attachments", []) if a.get("fileUrl")
        ]  # fmt: skip
        payload["conferences"] = _conferences(ev)
        google = {k: v for k, v in ev.items() if k not in _MAPPED}
        _drop_marker(google)
        if google.get("eventType") == "default":
            del google["eventType"]
        payload["provider_data"] = {"google": google}
        return payload


def _marker(ev: Mapping[str, Any]) -> str | None:
    """The ``usmp_id`` a USMP run stored on this event, if any."""
    value = ((ev.get("extendedProperties") or {}).get("private") or {}).get(_USMP_KEY)
    return str(value) if value else None


def _drop_marker(google: dict[str, Any]) -> None:
    """Remove USMP's own marker: it identifies the event and is not part of it."""
    props = google.get("extendedProperties")
    if not isinstance(props, dict) or _USMP_KEY not in (props.get("private") or {}):
        return
    private = {k: v for k, v in props["private"].items() if k != _USMP_KEY}
    props = {k: v for k, v in props.items() if k != "private"} | (
        {"private": private} if private else {}
    )
    if props:
        google["extendedProperties"] = props
    else:
        del google["extendedProperties"]


def _conferences(ev: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Join links: Meet and add-on conferences, phone numbers with their PINs."""
    found = []
    for entry in (ev.get("conferenceData") or {}).get("entryPoints", []):
        if not entry.get("uri"):
            continue
        label = entry.get("label")
        pin = entry.get("pin") or entry.get("passcode") or entry.get("accessCode")
        if pin and entry.get("entryPointType") == "phone":
            label = f"{label or entry['uri'].removeprefix('tel:')} PIN: {pin}"
        found.append({
            "uri": entry["uri"],
            "label": label,
            "features": _ENTRY_FEATURES.get(entry.get("entryPointType", ""), []),
        })  # fmt: skip
    if ev.get("hangoutLink") and ev["hangoutLink"] not in {c["uri"] for c in found}:
        found.append({"uri": ev["hangoutLink"], "features": ["video"]})
    return found


def _calendar_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": entry.get("summaryOverride") or entry.get("summary") or entry["id"]
    }
    if entry.get("description"):
        payload["description"] = entry["description"]
    if entry.get("backgroundColor"):
        payload["color"] = str(entry["backgroundColor"]).lower()
    zone = resolve_zone(entry.get("timeZone"))
    if zone:
        payload["time_zone"] = zone
    return payload


def _records_for_calendar(api: _Api, entry: Mapping[str, Any]) -> Iterator[ExportRecord]:
    cal_id = str(entry["id"])
    cal_item = calendar_item(NAME, cal_id, _calendar_payload(entry))
    yield ExportRecord(item=cal_item)
    converter = _EventConverter(
        _zone(cal_item.payload.get("time_zone")), entry.get("defaultReminders") or ()
    )
    raw_events = list(
        api.pages(f"/calendars/{quote(cal_id, safe='')}/events",
                  {"singleEvents": "false", "showDeleted": "false", "maxResults": 2500})
    )  # fmt: skip
    # Deleted occurrences of a series come back as cancelled instances: they are exclusions.
    cancelled: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for ev in raw_events:
        if ev.get("status") == "cancelled" and ev.get("recurringEventId"):
            cancelled[str(ev["recurringEventId"])].append(ev["originalStartTime"])
    for ev in raw_events:
        if ev.get("status") == "cancelled" and ev.get("recurringEventId"):
            continue
        native_id = f"{cal_id}/{ev['id']}"
        conv = Converter(native_id)
        payload = converter.convert(ev, conv)
        if ev["id"] in cancelled:
            zone = payload.get("time_zone")
            payload["excluded_dates"] = [
                *payload.get("excluded_dates", []),
                *(_to_local(o, zone)[0] for o in cancelled[ev["id"]]),
            ]
        try:
            item = event_item(NAME, native_id, payload, parent=cal_item.usmp_id)
        except ValueError as exc:
            raise ConnectorError(f"Google event {ev['id']} cannot be represented: {exc}") from exc
        losses = [
            LossRecord(usmp_id=item.usmp_id, field=f, reason=r, stage="export", detail=d)
            for f, r, d in conv.losses
        ]
        yield ExportRecord(
            item=item, losses=losses, raw=json.dumps(ev, sort_keys=True).encode(),
            raw_name=hashlib.sha256(native_id.encode()).hexdigest()[:32] + ".json",
        )  # fmt: skip


class _GoogleReader(Reader):
    def __init__(self, api: _Api, only: str | None) -> None:
        self._api = api
        self._only = only

    def read(self, domain: str) -> Iterator[ExportRecord]:
        if domain != "calendar":
            return
        entries = sorted(self._api.pages("/users/me/calendarList", {}), key=lambda e: e["id"])
        names = [_calendar_payload(e)["name"] for e in entries]
        if self._only and self._only not in names:
            raise ConnectorError(
                f"no calendar named {self._only!r}; available: {', '.join(sorted(names))}"
            )
        for entry in entries:
            if entry.get("accessRole") == "freeBusyReader":
                continue
            if self._only and _calendar_payload(entry)["name"] != self._only:
                continue
            yield from _records_for_calendar(self._api, entry)


# --- canonical -> Google event -----------------------------------------------------------------


def _google_time(value: str, zone: str | None) -> dict[str, str]:
    if "T" not in value:
        return {"date": value}
    dt = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone or "UTC"))
    return {"dateTime": dt.isoformat(), "timeZone": zone or "UTC"}


def _ical_time(value: str) -> str:
    if "T" not in value:
        return value.replace("-", "")
    return value.replace("-", "").replace(":", "")


def _google_event(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Google event resource for ``payload`` and ``(field, detail)`` for anything changed."""
    changed: list[tuple[str, str]] = []
    zone = payload.get("time_zone")
    ev: dict[str, Any] = {
        "iCalUID": payload["uid"],
        "start": _google_time(payload["start"], zone),
    }
    if "end" in payload:
        ev["end"] = _google_time(payload["end"], payload.get("end_time_zone", zone))
    elif "T" not in payload["start"]:  # Google needs an end: all-day events default to one day
        ev["end"] = {"date": (date.fromisoformat(payload["start"]) + timedelta(days=1)).isoformat()}
    else:  # timed events default to zero length
        ev["end"] = ev["start"]
    for key, name in (
        ("title", "summary"),
        ("description", "description"),
        ("location", "location"),
    ):
        if key in payload:
            ev[name] = payload[key]
    if "status" in payload:
        ev["status"] = payload["status"]
    if "availability" in payload:
        ev["transparency"] = "transparent" if payload["availability"] == "free" else "opaque"
    if "privacy" in payload:
        ev["visibility"] = payload["privacy"]
    lines = [f"RRULE:{r}" for r in payload.get("recurrence_rules", [])]
    tz_param = f";TZID={zone}" if zone and "T" in payload["start"] else ""
    date_param = ";VALUE=DATE" if "T" not in payload["start"] else ""
    for key, prop in (("recurrence_dates", "RDATE"), ("excluded_dates", "EXDATE")):
        if payload.get(key):
            values = ",".join(_ical_time(v) for v in payload[key])
            lines.append(f"{prop}{tz_param}{date_param}:{values}")
    if lines:
        ev["recurrence"] = lines
    if "organizer" in payload:
        ev["organizer"] = {"email": payload["organizer"]["email"],
                           **({"displayName": payload["organizer"]["name"]}
                              if payload["organizer"].get("name") else {})}  # fmt: skip
    attendees = []
    for att in payload.get("attendees", []):
        kind = att.get("kind", "individual")
        if kind in ("group", "unknown"):
            changed.append(("attendees", f"{att['email']}: Google has no {kind} attendees"))
        elif kind == "room":
            changed.append(("attendees", f"{att['email']}: Google stores rooms as resources"))
        role = att.get("role", "required")
        if role in _ROLE_LOSS:
            changed.append(("attendees", f"{att['email']}: Google has no {role} role"))
        status = att.get("status", "needs_action")
        if status == "delegated":
            changed.append(("attendees", f"{att['email']}: Google has no delegated status"))
        attendees.append({
            "email": att["email"],
            **({"displayName": att["name"]} if att.get("name") else {}),
            **({"optional": True} if role == "optional" else {}),
            **({"resource": True} if kind in ("resource", "room") else {}),
            "responseStatus": _STATUS_OUT.get(status, status),
        })  # fmt: skip
    if attendees:
        ev["attendees"] = attendees
    overrides = []
    for alarm in payload.get("alarms", []):
        minutes = _minutes_before(alarm["trigger"])
        if minutes is None or alarm.get("action") == "audio":
            changed.append(("alarms", f"Google cannot store the alarm {alarm}"))
            continue
        overrides.append({"method": "email" if alarm.get("action") == "email" else "popup",
                          "minutes": minutes})  # fmt: skip
    if len(overrides) > 5:
        changed.append(("alarms", "Google keeps at most 5 reminders"))
        overrides = overrides[:5]
    ev["reminders"] = {"useDefault": False, "overrides": overrides}
    changed += _restore_provider_data(payload, ev)
    return ev, changed


def _restore_provider_data(payload: Mapping[str, Any], ev: dict[str, Any]) -> list[tuple[str, str]]:
    """Put Google's own properties back, and add join details Google cannot attach natively."""
    changed: list[tuple[str, str]] = []
    data = payload.get("provider_data", {})
    google = dict(data.get("google", {}))
    others = sorted(set(data) - {"google"})
    if others:
        changed.append(("provider_data", f"details from {', '.join(others)} are not kept"))
    event_type = google.pop("eventType", "default")
    if event_type != "default":
        changed.append(("provider_data", f"Google only lets apps import ordinary events, so "
                        f"this {event_type} event becomes an ordinary event"))  # fmt: skip
        google.pop(f"{event_type}Properties", None)
    dropped = sorted(set(google) - _RESTORABLE)
    if dropped:
        changed.append(("provider_data", f"Google does not accept {', '.join(dropped)} here"))
    ev.update({k: v for k, v in google.items() if k in _RESTORABLE})
    if "conferenceData" in ev:
        ev["conferenceData"] = {
            k: v for k, v in ev["conferenceData"].items() if k != "createRequest"
        }
    conferences = list(payload.get("conferences", []))
    if conferences and "conferenceData" not in ev:
        # Google can only attach meetings it created; keep the links readable in the notes.
        notes = notes_with_conferences(payload.get("description"), conferences)
        if notes is not None:
            ev["description"] = notes
        changed.append(("conferences", "join links were added to the event notes"))
        if notes != payload.get("description"):
            changed.append(("description", "join links were added to the event notes"))
    return changed


def _minutes_before(trigger: str) -> int | None:
    """Minutes before the start for a canonical trigger, or ``None`` if Google can't store it."""
    if not trigger.startswith("-") and trigger != "PT0S":
        return None
    text = trigger.lstrip("-").removeprefix("P")
    days, _, clock = text.partition("T")
    total = int(days.rstrip("D") or 0) * 1440 if days else 0
    number = ""
    for ch in clock:
        if ch.isdigit():
            number += ch
            continue
        total += {"H": 60 * int(number), "M": int(number), "S": int(number) // 60}[ch]
        number = ""
    return total if total <= 40320 else None


def _instance_suffix(recurrence_id: str, zone: str | None) -> str:
    """Google instance ids end with the original start in UTC, or the date for all-day events."""
    if "T" not in recurrence_id:
        return recurrence_id.replace("-", "")
    dt = datetime.fromisoformat(recurrence_id).replace(tzinfo=ZoneInfo(zone or "UTC"))
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


class _GoogleWriter(Writer):
    def __init__(self, api: _Api, only: str | None) -> None:
        self._api = api
        self._only = only
        self._placed: dict[str, str] = {}  # calendar usmp_id -> calendar an earlier run made
        self._masters: dict[tuple[str, str], str] = {}  # (calendar, uid) -> series event id

    def _calendar_ids(self) -> dict[str, str]:
        return {
            _calendar_payload(e)["name"]: e["id"]
            for e in self._api.pages("/users/me/calendarList", {})
        }

    def _create_calendar(
        self, existing: dict[str, str], payload: Mapping[str, Any]
    ) -> tuple[str, str]:
        name, unique, n = str(payload["name"]), str(payload["name"]), 1
        while unique in existing:
            n += 1
            unique = f"{name} ({n})"
        body: dict[str, Any] = {"summary": unique}
        if payload.get("description"):
            body["description"] = payload["description"]
        if payload.get("time_zone"):
            body["timeZone"] = payload["time_zone"]
        known = set(existing.values())

        def find_created() -> dict[str, Any] | None:
            # A calendar with this name that was not there before is the one we just made.
            for entry in self._api.pages("/users/me/calendarList", {}):
                if entry.get("summary") == unique and entry["id"] not in known:
                    return dict(entry)
            return None

        cal_id = str(self._api.call("POST", "/calendars", body=body, recover=find_created)["id"])
        if payload.get("color"):
            colors = {"backgroundColor": payload["color"], "foregroundColor": "#000000"}
            self._api.call(
                "PATCH",
                f"/users/me/calendarList/{quote(cal_id, safe='')}",
                params={"colorRgbFormat": "true"},
                body=colors,
            )
        existing[unique] = cal_id
        return cal_id, unique

    def already_written(
        self, items: Sequence[Item], known: Mapping[str, WriteResult]
    ) -> Iterator[WriteResult]:
        entries = {e["id"]: e for e in self._api.pages("/users/me/calendarList", {})}
        events: dict[str, tuple[str, str]] = {}  # event usmp_id -> (calendar, native id)
        for cal_id, entry in sorted(entries.items()):
            if entry.get("accessRole") not in ("owner", "writer"):
                continue
            params = {"singleEvents": "false", "showDeleted": "false", "maxResults": 2500}
            for ev in self._api.pages(f"/calendars/{quote(cal_id, safe='')}/events", params):
                marker = _marker(ev)
                if marker and ev.get("status") != "cancelled":
                    events[marker] = (cal_id, f"{cal_id}/{ev['id']}")
                    if "recurringEventId" not in ev:
                        self._masters[(cal_id, str(ev.get("iCalUID")))] = str(ev["id"])
        holding: dict[str, str] = {}
        for item in items:
            if item.type == "event" and item.usmp_id in events and item.parent:
                holding.setdefault(item.parent, events[item.usmp_id][0])
        for item in items:
            if item.type == "calendar" and self._only is None:
                earlier = known.get(item.usmp_id)
                cal_id = (
                    earlier.native_id if earlier and earlier.native_id in entries else None
                ) or holding.get(item.usmp_id)
                if cal_id is None:
                    continue
                self._placed[item.usmp_id] = cal_id
                if earlier:
                    losses = list(earlier.losses)
                else:
                    name = _calendar_payload(entries[cal_id])["name"]
                    losses = (
                        [_import_loss(item, "name", f"stored as {name!r}", Severity.INFO)]
                        if name != item.payload["name"]
                        else []
                    )
                yield WriteResult(item.usmp_id, cal_id, tuple(losses))
            elif item.type == "event" and item.usmp_id in events:
                _, changed = _google_event(item.payload)
                losses = [_import_loss(item, f, d) for f, d in changed]
                yield WriteResult(item.usmp_id, events[item.usmp_id][1], tuple(losses))

    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        items = list(items)
        existing = self._calendar_ids()
        by_source: dict[str, str] = dict(self._placed)
        merged: str | None = None
        if self._only:
            merged = (
                existing.get(self._only) or self._create_calendar(existing, {"name": self._only})[0]
            )
        for item in items:
            if item.domain != "calendar" or item.type not in ("calendar", "event"):
                raise ConnectorError(
                    f"cannot write {item.domain}/{item.type} items to Google Calendar"
                )
            if item.type != "calendar":
                continue
            if merged is not None:
                cal_id, name = merged, self._only or ""
            else:
                cal_id, name = self._create_calendar(existing, item.payload)
            losses = []
            if name != item.payload["name"]:
                losses.append(_import_loss(item, "name", f"stored as {name!r}", Severity.INFO))
            if merged is not None:
                # Merging never changes the existing calendar's own settings.
                losses += [
                    _import_loss(item, key, f"merged into the existing calendar {name!r}")
                    for key in ("description", "color", "time_zone")
                    if key in item.payload
                ]
            by_source[item.usmp_id] = cal_id
            yield WriteResult(item.usmp_id, cal_id, tuple(losses))

        masters = self._masters
        ordered = sorted(
            (i for i in items if i.type == "event"), key=lambda i: "recurrence_id" in i.payload
        )
        for item in ordered:
            found = by_source.get(item.parent or "") or merged
            if found is None:
                found = merged = self._create_calendar(existing, {"name": "Imported"})[0]
            cal_id = found
            body, changed = _google_event(item.payload)
            private = body.setdefault("extendedProperties", {}).setdefault("private", {})
            body["extendedProperties"] = {
                **body["extendedProperties"],
                "private": {**private, _USMP_KEY: item.usmp_id},
            }
            path_cal = quote(cal_id, safe="")
            if "recurrence_id" in item.payload:
                master = masters.get((cal_id, str(item.payload["uid"])))
                if master is None:
                    raise ConnectorError(
                        f"occurrence of {item.payload['uid']} has no series to attach to"
                    )
                suffix = _instance_suffix(
                    str(item.payload["recurrence_id"]), _zone(item.payload.get("time_zone"))
                )
                instance = f"{master}_{suffix}"
                body.pop("iCalUID")
                body.pop("recurrence", None)
                saved = self._api.call(
                    "PUT", f"/calendars/{path_cal}/events/{instance}", params=_CONF, body=body
                )
            else:
                saved = self._api.call(
                    "POST",
                    f"/calendars/{path_cal}/events/import",
                    params=_CONF,
                    body=body,
                    recover=self._finder(cal_id, str(item.payload["uid"]), item.usmp_id),
                )
                masters[(cal_id, str(item.payload["uid"]))] = str(saved["id"])
            losses = [_import_loss(item, f, d) for f, d in changed]
            yield WriteResult(item.usmp_id, f"{cal_id}/{saved['id']}", tuple(losses))

    def _finder(self, cal_id: str, uid: str, usmp_id: str) -> Callable[[], Any]:
        """Find the series this run imported, if a try whose answer was lost stored it."""

        def find() -> dict[str, Any] | None:
            params = {"iCalUID": uid, "showDeleted": "false"}
            for ev in self._api.pages(f"/calendars/{quote(cal_id, safe='')}/events", params):
                if (
                    ev.get("iCalUID") == uid
                    and "recurringEventId" not in ev
                    and _marker(ev) == usmp_id
                ):
                    return dict(ev)
            return None

        return find

    def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
        entries = {e["id"]: e for e in self._api.pages("/users/me/calendarList", {})}
        wanted = set(native_ids)
        calendars = {n.split("/", 1)[0] for n in native_ids}
        for cal_id in sorted(calendars):
            entry = entries.get(cal_id)
            if entry is None:
                continue
            for record in _records_for_calendar(self._api, entry):
                if record.item.source.native_id in wanted:
                    yield record.item


def _import_loss(
    item: Item, field: str, detail: str, severity: Severity = Severity.WARNING
) -> LossRecord:
    return LossRecord(usmp_id=item.usmp_id, field=field, reason=LossReason.VALUE_CONVERTED,
                      severity=severity, stage="import", detail=detail)  # fmt: skip


# --- Authentication ----------------------------------------------------------------------------


def token_account(account: str, write: bool) -> str:
    return f"{account}:{'write' if write else 'read'}"


def sign_in(client_secrets: str, *, write: bool, open_browser: bool = True) -> tuple[str, str]:
    """Run the browser sign-in and return ``(account email, credentials JSON)``."""
    transport = _require_google()
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

    flow = InstalledAppFlow.from_client_secrets_file(
        client_secrets,
        scopes=[WRITE_SCOPE if write else READ_SCOPE],
        autogenerate_code_verifier=True,
    )
    creds = flow.run_local_server(
        port=0,
        open_browser=open_browser,
        authorization_prompt_message="Sign in here: {url}",
        success_message="Signed in to USMP. You can close this window.",
    )
    session = transport.AuthorizedSession(creds)
    primary = _Api(session, read_only=True).call("GET", "/calendars/primary")
    return str(primary["id"]), str(creds.to_json())


def session_factory(account: str, *, write: bool) -> Callable[[], Any]:
    """Build an authorised HTTP session from the keychain, refreshing the token as needed."""

    def make() -> Any:
        transport = _require_google()
        from google.oauth2.credentials import Credentials

        stored = credentials.get_secret(NAME, token_account(account, write))
        if stored is None and not write:
            stored = credentials.get_secret(NAME, token_account(account, True))
        if stored is None:
            flag = " --write" if write else ""
            raise ConnectorError(
                f"not signed in to Google as {account}: run "
                f"`usmp auth google --client-secrets client_secret.json{flag}`"
            )
        info = json.loads(stored)
        creds = Credentials.from_authorized_user_info(info)  # type: ignore[no-untyped-call]
        if not creds.valid:
            try:
                creds.refresh(transport.Request())
            except Exception as exc:  # google-auth raises several unrelated types
                raise ConnectorError(f"cannot refresh the Google sign-in: {exc}") from exc
            credentials.set_secret(NAME, token_account(account, write), creds.to_json())
        return transport.AuthorizedSession(creds)

    return make


class GoogleCalendarConnector(Connector):
    name = NAME
    display_name = "Google Calendar"
    description = "Google Calendar via the Calendar API. Needs: pip install usmp-protocol[google]"
    config_keys: ClassVar[Mapping[str, str]] = {
        "account": "your Google account email, as used with `usmp auth google`",
        "calendar": "only read this calendar / write everything into this calendar",
    }
    #: Replaced in tests with a factory returning a fake session.
    session_for: ClassVar[Callable[[str, bool], Callable[[], Any]]] = staticmethod(
        lambda account, write: session_factory(account, write=write)
    )

    def capabilities(self) -> CapabilityManifest:
        return capabilities()

    def info(self) -> Any:
        info = super().info()
        return info if info.label else info.model_copy(update={"label": self.config.get("account")})

    def open_reader(self) -> Reader:
        session = type(self).session_for(self.require("account"), False)()
        return _GoogleReader(_Api(session, read_only=True), self.config.get("calendar"))

    def open_writer(self) -> Writer:
        session = type(self).session_for(self.require("account"), True)()
        return _GoogleWriter(_Api(session, read_only=False), self.config.get("calendar"))


__all__ = ["GoogleCalendarConnector", "credentials"]
