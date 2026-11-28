"""Which calendar items a :class:`~usmp.core.models.Selection` keeps (spec §8.2).

A series is never cut: an event is kept with its whole series (the master and every overridden
occurrence sharing its calendar and ``uid``) when any part of the series falls in the date range.
Calendars are kept when they are named in the selection, or (without names) when they still
hold a kept event. Matching errs on the side of keeping: an event whose times cannot be worked
out is kept.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from usmp.core.models import Item, Selection

Point = datetime
"""Aware for zoned times; naive (wall-clock) for all-day and floating times."""


def _point(value: str, zone: str | None) -> Point:
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    moment = datetime.fromisoformat(value)
    return moment.replace(tzinfo=ZoneInfo(zone)) if zone else moment


def _like(bound: datetime, point: Point) -> datetime:
    """``bound`` comparable with ``point``: as is for zoned points, else its own wall-clock time."""
    return bound if point.tzinfo is not None else bound.replace(tzinfo=None)


def _wall(bound: datetime, start: Point) -> datetime:
    """``bound`` as wall-clock time in ``start``'s zone (or its own, for floating times)."""
    return (
        bound.astimezone(start.tzinfo).replace(tzinfo=None) if start.tzinfo else _like(bound, start)
    )


def _overlaps(start: Point, end: Point, selection: Selection) -> bool:
    if selection.until is not None and not start < _like(selection.until, start):
        return False
    if selection.since is None:
        return True
    since = _like(selection.since, start)
    return end > since or (end == start and start >= since)


def _rule_text(rule: str, start: Point) -> str:
    """``rule`` with a UTC ``UNTIL`` turned into wall-clock time like the start, for dateutil."""
    parts = []
    for part in rule.split(";"):
        key, _, value = part.partition("=")
        if key == "UNTIL" and value.endswith("Z"):
            until = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            local = until.astimezone(start.tzinfo) if start.tzinfo else until
            value = local.strftime("%Y%m%dT%H%M%S")
        parts.append(f"{key}={value}")
    return ";".join(parts)


def _series_overlaps(
    payload: Mapping[str, Any], start: Point, length: timedelta, selection: Selection
) -> bool:
    zone = payload.get("time_zone")
    for extra in payload.get("recurrence_dates", []):
        moment = _point(extra, zone)
        if _overlaps(moment, moment + length, selection):
            return True
    wall_start = start.replace(tzinfo=None)
    low = _wall(selection.since, start) - length if selection.since is not None else None
    high = _wall(selection.until, start) if selection.until is not None else None
    for rule in payload.get("recurrence_rules", []):
        recurrence = rrulestr(_rule_text(rule, start), dtstart=wall_start)
        first = (
            next(iter(recurrence), None) if low is None else recurrence.after(low, inc=not length)
        )
        if first is not None and (high is None or first < high):
            return True
    return False


def _event_overlaps(payload: Mapping[str, Any], selection: Selection) -> bool:
    try:
        start = _point(payload["start"], payload.get("time_zone"))
        if "end" in payload:
            end = _point(payload["end"], payload.get("end_time_zone") or payload.get("time_zone"))
        else:
            end = start + timedelta(days=1) if len(payload["start"]) == 10 else start
        length = end - start if (end.tzinfo is None) == (start.tzinfo is None) else timedelta()
        if _overlaps(start, start + length, selection):
            return True
        if payload.get("recurrence_rules") or payload.get("recurrence_dates"):
            return _series_overlaps(payload, start, max(length, timedelta()), selection)
    except (ValueError, TypeError, OverflowError, KeyError):
        return True
    return False


def select(items: Iterable[Item], selection: Selection) -> set[str]:
    """The ``usmp_id`` of every item ``selection`` keeps.

    Raises ``ValueError`` if the selection names a calendar that is not there.
    """
    items = list(items)
    calendars = {i.usmp_id: i for i in items if i.type == "calendar"}
    if selection.collections:
        by_name: dict[str, list[str]] = defaultdict(list)
        for cal in calendars.values():
            by_name[str(cal.payload.get("name", "")).casefold()].append(cal.usmp_id)
        unknown = [n for n in selection.collections if n.casefold() not in by_name]
        if unknown:
            available = ", ".join(sorted(str(c.payload.get("name")) for c in calendars.values()))
            raise ValueError(f"no calendar named {unknown[0]!r}; available: {available or 'none'}")
        named = {cid for n in selection.collections for cid in by_name[n.casefold()]}
    else:
        named = set(calendars)

    series: dict[tuple[str | None, str], list[Item]] = defaultdict(list)
    for item in items:
        if item.type == "event" and (not selection.collections or item.parent in named):
            series[(item.parent, str(item.payload.get("uid")))].append(item)
    dated = selection.since is not None or selection.until is not None
    events = {
        e.usmp_id
        for members in series.values()
        if not dated or any(_event_overlaps(e.payload, selection) for e in members)
        for e in members
    }
    if selection.collections:
        kept_calendars = named
    elif dated:
        kept_calendars = {i.parent for i in items if i.usmp_id in events and i.parent in calendars}
    else:
        kept_calendars = set(calendars)
    others = {i.usmp_id for i in items if i.type not in ("calendar", "event")}
    return events | {c for c in kept_calendars if c is not None} | others
