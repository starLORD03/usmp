"""A synthetic test calendar: realistic, varied and fully deterministic.

Used for the release test (1,000 events along every supported path) and for manual checks
against real accounts::

    python -m usmp.testing.synthetic test-calendar.ics --events 1000

Every event is synthetic: names, addresses (``example.com``) and links are made up.
"""

import argparse
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from usmp.core.models import Item
from usmp.domains.calendar import calendar_item, event_item

ZONES = [
    "UTC", "Europe/London", "Europe/Paris", "America/New_York", "America/Los_Angeles",
    "Asia/Kolkata", "Asia/Tokyo", "Australia/Sydney",
]  # fmt: skip
TITLES = [
    "Standup", "1:1 with Sam", "Design review", "Dentist", "Flight to Lisbon", "Team lunch",
    "Quarterly planning", "Yoga", "Café ☕ with Zoë", "Release: v2.0; final checks",
    "Parent-teacher meeting", "Gym", "Doctor's appointment", "日本語の会議", "Book club",
]  # fmt: skip
NOTES = [
    "Agenda:\n1. Status\n2. Risks, blockers; next steps",
    "Bring the printed tickets.",
    "Room changed \u2013 see the invite.",
    "Notes with commas, semicolons; and a backslash \\ in them",
    "🎉 Celebration!",
]
PLACES = ["Room 4.01", "Main office, Floor 2", "Online", "Café Central, Vienna", "東京オフィス"]
RULES = [
    "FREQ=DAILY;COUNT=10",
    "FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=30",
    "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU",
    "FREQ=MONTHLY;BYMONTHDAY=15;COUNT=12",
    "FREQ=MONTHLY;BYDAY=1MO",
    "FREQ=YEARLY",
]
START = date(2025, 1, 6)


def _people(rng: random.Random) -> list[dict[str, object]]:
    people: list[dict[str, object]] = [
        {
            "email": f"person{n}@example.com",
            "name": f"Person {n}",
            "role": rng.choice(["required", "required", "optional"]),
            "status": rng.choice(["needs_action", "accepted", "declined", "tentative"]),
        }
        for n in rng.sample(range(40), rng.randint(1, 4))
    ]
    if rng.random() < 0.2:
        people.append(
            {"email": f"room{rng.randint(1, 5)}@rooms.example.com", "name": "Meeting room",
             "kind": "room", "status": "accepted"}
        )  # fmt: skip
    return people


def _meeting(rng: random.Random) -> list[dict[str, object]]:
    code = "".join(rng.choice("abcdefghijkmnopqrstuvwxyz") for _ in range(10))
    link = f"https://meet.example.com/{code[:3]}-{code[3:7]}-{code[7:]}"
    return [
        {"uri": link, "label": link.removeprefix("https://"), "features": ["video"]},
        {
            "uri": f"tel:+1-555-01{rng.randint(10, 99)}",
            "label": f"+1 555-01{rng.randint(10, 99)} PIN: {rng.randint(100000, 999999)}",
            "features": ["phone"],
        },
    ]


def _timed(day: date, rng: random.Random) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, rng.randint(6, 20), rng.choice([0, 15, 30, 45]))
    end = start + timedelta(minutes=rng.choice([15, 30, 45, 60, 90, 120]))
    return start.isoformat(), end.isoformat()


def _event(n: int, rng: random.Random) -> dict[str, object]:
    day = START + timedelta(days=rng.randint(0, 900))
    payload: dict[str, object] = {
        "uid": f"synthetic-{n:05d}@usmp.example",
        "title": rng.choice(TITLES),
    }
    kind = rng.random()
    if kind < 0.15:  # all-day, one to five days
        payload["start"] = day.isoformat()
        payload["end"] = (day + timedelta(days=rng.randint(1, 5))).isoformat()
        payload["availability"] = rng.choice(["free", "busy"])
    else:
        payload["start"], payload["end"] = _timed(day, rng)
        payload["time_zone"] = rng.choice(ZONES)
        if kind > 0.97:  # a flight: ends in another zone
            payload["end_time_zone"] = rng.choice([z for z in ZONES if z != payload["time_zone"]])
            payload["end"] = (
                datetime.fromisoformat(str(payload["start"])) + timedelta(hours=9)
            ).isoformat()
    if rng.random() < 0.5:
        payload["description"] = rng.choice(NOTES)
    if rng.random() < 0.4:
        payload["location"] = rng.choice(PLACES)
    if rng.random() < 0.4:
        payload["organizer"] = {"email": "me@example.com", "name": "Me"}
        payload["attendees"] = _people(rng)
    if rng.random() < 0.25:
        payload["conferences"] = _meeting(rng)
    if rng.random() < 0.6:
        payload["alarms"] = [
            {"trigger": rng.choice(["-PT10M", "-PT30M", "-PT1H", "-P1D"]),
             "action": rng.choice(["display", "display", "email"])}
        ]  # fmt: skip
    if rng.random() < 0.1:
        payload["status"] = "tentative"
    if rng.random() < 0.1:
        payload["privacy"] = "private"
    return payload


def calendar_items(
    events: int = 1000, *, seed: int = 2026, connector: str = "synthetic"
) -> list[Item]:
    """A calendar and ``events`` event items (recurring series and moved occurrences included)."""
    rng = random.Random(seed)  # noqa: S311 - test data, not security
    cal = calendar_item(
        connector, "synthetic", {"name": "USMP test calendar", "color": "#1a73e8",
                                 "description": "Synthetic data for migration tests"},
    )  # fmt: skip
    items: list[Item] = [cal]

    def add(native_id: str, payload: dict[str, object]) -> None:
        items.append(event_item(connector, native_id, payload, parent=cal.usmp_id))

    n = 0
    while len(items) - 1 < events:
        payload = _event(n, rng)
        uid = str(payload["uid"])
        n += 1
        if "end_time_zone" in payload or rng.random() >= 0.12:
            add(uid, payload)
            continue
        timed = "T" in str(payload["start"])
        if not (timed and rng.random() < 0.5 and len(items) + 1 < events):
            payload["recurrence_rules"] = [rng.choice(RULES)]
            add(uid, payload)
            continue
        # A daily series whose second occurrence moved an hour later and third was cancelled.
        first = datetime.fromisoformat(str(payload["start"]))
        payload["recurrence_rules"] = ["FREQ=DAILY;COUNT=10"]
        payload["excluded_dates"] = [(first + timedelta(days=2)).isoformat()]
        add(uid, payload)
        series_only = ("recurrence_rules", "excluded_dates", "end")
        moved = {k: v for k, v in payload.items() if k not in series_only}
        moved["title"] = f"{payload['title']} (moved)"
        moved["recurrence_id"] = (first + timedelta(days=1)).isoformat()
        moved["start"] = (first + timedelta(days=1, hours=1)).isoformat()
        add(f"{uid}#2", moved)
    return items


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Write a synthetic test calendar as .ics")
    parser.add_argument("path", type=Path, help="file to create, e.g. test-calendar.ics")
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args(argv)
    from usmp.connectors._ical import build_vcalendar

    if args.path.exists():
        parser.error(f"{args.path} already exists")
    items = calendar_items(args.events, seed=args.seed)
    data = build_vcalendar(
        [i.payload for i in items if i.type == "event"],
        {"x-wr-calname": str(items[0].payload["name"])},
    )
    args.path.write_bytes(data)
    print(f"Wrote {len(items) - 1} events to {args.path}")


if __name__ == "__main__":  # pragma: no cover
    main()
