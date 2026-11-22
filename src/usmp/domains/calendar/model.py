"""The calendar domain, ``usmp.calendar`` schema version 1.

Aligned with iCalendar (RFC 5545) and JSCalendar (RFC 8984). See ``spec/domains/calendar.md``.

Two item types:

* ``calendar``: a collection of events.
* ``event``: one event. A recurring event is one item with recurrence rules. An instance whose
  details were changed ("this occurrence only") is a separate ``event`` item with the same ``uid``
  and a ``recurrence_id`` naming the occurrence it replaces.

Payloads must be in **canonical form** (see :func:`canonicalize`) so that the same event read from
two providers hashes the same. Canonical form omits absent and default values, normalises case
and ordering, and uses the time formats below.
"""

import functools
import json
import re
from datetime import date, datetime
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

DOMAIN = "calendar"
SCHEMA_VERSION = 1

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOCAL_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_DURATION = re.compile(
    r"^[+-]?P(?=\d|T\d)(?:(?P<W>\d+)W)?(?:(?P<D>\d+)D)?"
    r"(?:T(?=\d)(?:(?P<H>\d+)H)?(?:(?P<M>\d+)M)?(?:(?P<S>\d+)S)?)?$"
)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
_COLOR = re.compile(r"^#[0-9a-f]{6}$")
_UTC_ALIASES = frozenset({"UTC", "Etc/UTC", "Etc/GMT", "GMT", "Etc/Universal", "Z", "Zulu"})
_RRULE_FREQ = frozenset({"SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"})
_RRULE_KEYS = frozenset(
    {
        "FREQ", "UNTIL", "COUNT", "INTERVAL", "BYSECOND", "BYMINUTE", "BYHOUR", "BYDAY",
        "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH", "BYSETPOS", "WKST",
    }
)  # fmt: skip


# --- Normalisers -------------------------------------------------------------------------------


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


@functools.lru_cache(maxsize=1024)
def _check_zone(name: str) -> str:
    if name in _UTC_ALIASES:
        return "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA time zone {name!r}") from exc
    return name


def _date_or_datetime(value: str) -> str:
    """Validate ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS`` (no offset: the zone is separate)."""
    if _DATE.fullmatch(value):
        date.fromisoformat(value)
    elif _LOCAL_DATETIME.fullmatch(value):
        datetime.fromisoformat(value)
    else:
        raise ValueError(
            f"{value!r} must be a date (YYYY-MM-DD) or a local date-time (YYYY-MM-DDTHH:MM:SS) "
            "without an offset; put the time zone in time_zone"
        )
    return value


def _normalise_rrule(value: str) -> str:
    """Canonical RRULE: upper case, ``FREQ`` first, other parts sorted by name."""
    text = value.strip().upper().removeprefix("RRULE:")
    parts: dict[str, str] = {}
    for part in filter(None, text.split(";")):
        key, sep, val = part.partition("=")
        if not sep or not val:
            raise ValueError(f"invalid recurrence rule part {part!r}")
        if key not in _RRULE_KEYS:
            raise ValueError(f"unknown recurrence rule part {key!r}")
        if key in parts:
            raise ValueError(f"recurrence rule repeats {key!r}")
        parts[key] = val
    if parts.get("FREQ") not in _RRULE_FREQ:
        raise ValueError(f"recurrence rule {value!r} needs a valid FREQ")
    if "UNTIL" in parts and "COUNT" in parts:
        raise ValueError("recurrence rule cannot have both UNTIL and COUNT")
    ordered = ["FREQ=" + parts.pop("FREQ"), *(f"{k}={parts[k]}" for k in sorted(parts))]
    return ";".join(ordered)


def _normalise_email(value: str) -> str:
    value = value.strip().removeprefix("mailto:").removeprefix("MAILTO:").lower()
    if not _EMAIL.fullmatch(value):
        raise ValueError(f"{value!r} is not an email address")
    return value


def _normalise_color(value: str) -> str:
    value = value.strip().lower()
    if not _COLOR.fullmatch(value):
        raise ValueError(f"{value!r} must be a colour like #1a73e8")
    return value


def _normalise_duration(value: str) -> str:
    """Canonical duration: days and time only (``P1W`` becomes ``P7D``), largest units first."""
    text = value.strip().upper()
    match = _DURATION.fullmatch(text)
    if not match:
        raise ValueError(f"{value!r} is not an ISO 8601 duration such as -PT15M")
    weeks, days, hours, minutes, seconds = (int(match[unit] or 0) for unit in "WDHMS")
    total = (((weeks * 7 + days) * 24 + hours) * 60 + minutes) * 60 + seconds
    sign = "-" if text.startswith("-") and total else ""
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    time = "".join(f"{n}{unit}" for n, unit in ((h, "H"), (m, "M"), (s, "S")) if n)
    if not d and not time:
        return "PT0S"
    return f"{sign}P{f'{d}D' if d else ''}{f'T{time}' if time else ''}"


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted(set(values))


Text = Annotated[str | None, BeforeValidator(_blank_to_none)]
TimeZone = Annotated[str, AfterValidator(_check_zone), Field(description="IANA time zone name.")]
DateOrDateTime = Annotated[
    str,
    AfterValidator(_date_or_datetime),
    Field(description="`YYYY-MM-DD` (all day) or `YYYY-MM-DDTHH:MM:SS` (local to time_zone)."),
]
Email = Annotated[str, AfterValidator(_normalise_email)]
Color = Annotated[str, AfterValidator(_normalise_color)]
RRule = Annotated[str, AfterValidator(_normalise_rrule)]
Duration = Annotated[str, AfterValidator(_normalise_duration)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Item types --------------------------------------------------------------------------------


class Calendar(_Model):
    """A collection of events."""

    name: str = Field(min_length=1)
    description: Text = None
    color: Color | None = None
    time_zone: TimeZone | None = Field(default=None, description="Default zone for new events.")


class Person(_Model):
    email: Email
    name: Text = None


class Attendee(Person):
    kind: Literal["individual", "group", "resource", "room", "unknown"] = Field(
        default="individual", description="What the attendee is (iCalendar `CUTYPE`)."
    )
    role: Literal["chair", "required", "optional", "non_participant"] = "required"
    status: Literal["needs_action", "accepted", "declined", "tentative", "delegated"] = (
        "needs_action"
    )


class Alarm(_Model):
    trigger: Duration = Field(description="Offset from the start, e.g. `-PT15M`.")
    action: Literal["display", "email", "audio"] = "display"


class Attachment(_Model):
    uri: str = Field(min_length=1, description="Where the file lives. Files are not copied.")
    media_type: Text = None
    title: Text = None


class Conference(_Model):
    """A way to join the event online or by phone (iCalendar `CONFERENCE`, RFC 7986)."""

    uri: str = Field(min_length=1, description="Join link, e.g. `https://meet.example/abc`.")
    label: Text = None
    features: Annotated[
        list[Literal["audio", "chat", "feed", "moderator", "phone", "screen", "video"]],
        AfterValidator(_sorted_unique),
    ] = Field(default_factory=list)


ProviderName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]


class Event(_Model):
    """One event, or one overridden occurrence of a recurring event."""

    uid: str = Field(min_length=1, description="iCalendar UID, shared by all occurrences.")
    title: Text = None
    description: Text = None
    location: Text = None
    url: Text = None
    start: DateOrDateTime
    end: DateOrDateTime | None = Field(
        default=None, description="Exclusive end, same form as `start`."
    )
    time_zone: TimeZone | None = Field(
        default=None,
        description="Zone of `start` (and `end`, unless `end_time_zone` is set). Absent for "
        "all-day events and for floating times.",
    )
    end_time_zone: TimeZone | None = None
    status: Literal["confirmed", "tentative", "cancelled"] | None = None
    availability: Literal["busy", "free"] | None = None
    privacy: Literal["public", "private", "confidential"] | None = None
    recurrence_rules: list[RRule] = Field(default_factory=list)
    recurrence_dates: Annotated[list[DateOrDateTime], AfterValidator(_sorted_unique)] = Field(
        default_factory=list
    )
    excluded_dates: Annotated[list[DateOrDateTime], AfterValidator(_sorted_unique)] = Field(
        default_factory=list
    )
    recurrence_id: DateOrDateTime | None = Field(
        default=None,
        description="Set on an overridden occurrence: the original start it replaces.",
    )
    organizer: Person | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    alarms: list[Alarm] = Field(default_factory=list)
    categories: Annotated[list[str], AfterValidator(_sorted_unique)] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    color: Color | None = None
    conferences: list[Conference] = Field(default_factory=list)
    provider_data: dict[ProviderName, dict[str, JsonValue]] = Field(
        default_factory=dict,
        description="Provider-specific details with no place in the model, keyed by connector "
        "name, so the event can be restored exactly on the same provider.",
    )

    @field_validator("attendees")
    @classmethod
    def _attendees_by_email(cls, value: list[Attendee]) -> list[Attendee]:
        emails = [a.email for a in value]
        if len(emails) != len(set(emails)):
            raise ValueError("attendees must have unique email addresses")
        return sorted(value, key=lambda a: a.email)

    @field_validator("alarms")
    @classmethod
    def _alarms_sorted(cls, value: list[Alarm]) -> list[Alarm]:
        return sorted(set(value), key=lambda a: (a.trigger, a.action))

    @field_validator("attachments")
    @classmethod
    def _attachments_sorted(cls, value: list[Attachment]) -> list[Attachment]:
        return sorted(set(value), key=lambda a: a.uri)

    @field_validator("conferences")
    @classmethod
    def _conferences_sorted(cls, value: list[Conference]) -> list[Conference]:
        unique = {(c.uri, json.dumps(c.model_dump(mode="json"), sort_keys=True)): c for c in value}
        return [unique[k] for k in sorted(unique)]

    @field_validator("provider_data")
    @classmethod
    def _drop_empty_providers(
        cls, value: dict[str, dict[str, JsonValue]]
    ) -> dict[str, dict[str, JsonValue]]:
        return {k: v for k, v in sorted(value.items()) if v}

    @property
    def all_day(self) -> bool:
        return bool(_DATE.fullmatch(self.start))

    @model_validator(mode="after")
    def _consistent_times(self) -> Self:
        kind = "date" if self.all_day else "date-time"

        def same_kind(value: str, what: str) -> None:
            if bool(_DATE.fullmatch(value)) != self.all_day:
                raise ValueError(f"{what} must be a {kind} like start")

        if self.end is not None:
            same_kind(self.end, "end")
            if self.end_time_zone in (None, self.time_zone) and self.end <= self.start:
                raise ValueError("end must be after start")
        for value in [*self.recurrence_dates, *self.excluded_dates]:
            same_kind(value, "recurrence and excluded dates")
        if self.recurrence_id is not None:
            same_kind(self.recurrence_id, "recurrence_id")
            if self.recurrence_rules:
                raise ValueError("an overridden occurrence cannot have recurrence rules")
        if self.all_day and (self.time_zone or self.end_time_zone):
            raise ValueError("all-day events have no time zone")
        if self.end_time_zone and self.end is None:
            raise ValueError("end_time_zone needs end")
        if self.end_time_zone == self.time_zone and self.end_time_zone is not None:
            raise ValueError("omit end_time_zone when it equals time_zone")
        return self


ITEM_TYPES: dict[str, type[_Model]] = {"calendar": Calendar, "event": Event}
