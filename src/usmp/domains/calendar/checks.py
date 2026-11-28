"""Odd event times worth showing before a migration (spec ``calendar.md`` §9).

Real calendars sometimes hold events that end before they start, or that run for weeks by
mistake. USMP migrates them exactly as they are (changing them would be a silent loss) but says
so in the preview and the report, so the person can fix them at either end.

Within one time zone the calendar model already requires the end to be after the start, and
readers move such events without their end, declaring the loss. What is left to catch here is an
end in another zone that falls before the start, and timed events that last over a week.
"""

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from usmp.core.models import DataWarning, Item

LONG_TIMED = timedelta(days=7)
"""A timed event longer than this is probably a mistake (an all-day event is not checked)."""


def _when(value: str, zone: str | None) -> date | datetime:
    if len(value) == 10:
        return date.fromisoformat(value)
    moment = datetime.fromisoformat(value)
    return moment.replace(tzinfo=ZoneInfo(zone)) if zone else moment


def _hint(payload: Mapping[str, object]) -> str:
    zone = payload.get("time_zone")
    return f"starts {payload['start']}" + (f" ({zone})" if zone else "")


def _span(length: timedelta) -> str:
    hours = abs(length).total_seconds() / 3600
    if hours < 48:
        return f"{hours:g} hour{'s' if hours != 1 else ''}"
    return f"{hours / 24:.0f} days"


def warnings(items: Iterable[Item]) -> list[DataWarning]:
    """Events whose end is before their start, or that last implausibly long."""
    found = []
    for item in items:
        payload = dict(item.payload)
        if item.type != "event" or "end" not in payload:
            continue
        zone = payload.get("time_zone")
        end_zone = payload.get("end_time_zone") or zone
        try:
            start = _when(str(payload["start"]), str(zone) if zone else None)
            end = _when(str(payload["end"]), str(end_zone) if end_zone else None)
        except (ValueError, ZoneInfoNotFoundError):
            continue  # not valid calendar data: reported by validation instead
        if isinstance(start, datetime) and isinstance(end, datetime):
            if (start.tzinfo is None) != (end.tzinfo is None):
                continue
            length = end - start
        elif not isinstance(start, datetime) and not isinstance(end, datetime):
            length = end - start
        else:
            continue
        if length < timedelta(0):
            detail = f"ends {_span(length)} before it starts"
        elif isinstance(start, datetime) and length > LONG_TIMED:
            detail = f"lasts {_span(length)}: check its end"
        else:
            continue
        warning = DataWarning(usmp_id=item.usmp_id, field="end", detail=detail, hint=_hint(payload))
        found.append(warning)
    return found
