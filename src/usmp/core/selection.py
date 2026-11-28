"""Turn ``--since``/``--until``/``--calendar`` answers into a :class:`Selection`."""

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from pydantic import ValidationError

from usmp.core.models import Selection


class SelectionError(ValueError):
    """The selection cannot be understood."""


def _bound(text: str, *, end: bool) -> datetime:
    """``text`` as an aware instant. A date means its start, or for ``end`` the next day's start
    (so ``--until 2025-12-31`` includes that day). Times without an offset are local."""
    text = text.strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        pass
    else:
        midnight = datetime.combine(day + timedelta(days=1) if end else day, time())
        return midnight.astimezone()
    try:
        moment = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        raise SelectionError(
            f"{text!r} is not a date (YYYY-MM-DD) or date and time (YYYY-MM-DDTHH:MM)"
        ) from None
    return moment if moment.tzinfo is not None else moment.astimezone()


def make_selection(
    since: str | None = None, until: str | None = None, collections: Iterable[str] = ()
) -> Selection | None:
    """The selection for these answers, or ``None`` when they select everything."""
    names = [n.strip() for n in collections if n.strip()]
    try:
        selection = Selection(
            since=_bound(since, end=False) if since else None,
            until=_bound(until, end=True) if until else None,
            collections=names,
        )
    except ValidationError as exc:
        raise SelectionError(
            "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
        ) from None
    return None if selection.empty else selection
