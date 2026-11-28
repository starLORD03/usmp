"""Retrying requests that fail for a moment: rate limits, busy servers, dropped connections.

Connectors decide *what* is worth retrying, because only they know whether a request is safe to
send twice. This module decides *how*: exponential back-off with full jitter, an honoured
``Retry-After``, and limits on both the number of attempts and the total time spent waiting.

A request is safe to repeat when repeating it cannot change the result: reads, a ``PUT`` of the
same content to the same place, a create that the server refuses to repeat (``If-None-Match: *``,
or an import keyed by a UID). A create that could land twice must only be retried on answers that
mean "not done" (429 Too Many Requests), or its connector must look for its own earlier write
(by ``usmp_id``) before trying again.

Usage::

    def attempt() -> Response:
        response = session.get(url)
        if response.status_code in (429, 503):
            raise TransientError(f"HTTP {response.status_code}",
                                 retry_after=parse_retry_after(response.headers.get("Retry-After")))
        return response

    response = retrying(attempt, what="reading the calendar list")
"""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

from usmp.sdk.connector import ConnectorError

T = TypeVar("T")


class TransientError(Exception):
    """Raised by a retried operation when trying again later may succeed."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        """Seconds the server asked us to wait, if it said."""


@dataclass(frozen=True)
class RetryPolicy:
    """How often and how long to retry."""

    attempts: int = 6
    """Total tries, including the first."""
    base: float = 1.0
    """Upper bound of the first back-off, in seconds; doubles on every retry."""
    cap: float = 60.0
    """Largest back-off between two tries, unless the server asks for longer."""
    max_wait: float = 300.0
    """Most time spent waiting across all retries of one operation."""
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)

    def backoff(self, retry: int, retry_after: float | None) -> float:
        """Seconds to wait before retry number ``retry`` (0 for the first retry).

        Full jitter (a random wait between 0 and the exponential bound) spreads out clients that
        failed together. A server's ``Retry-After`` is a minimum.
        """
        bound = min(self.cap, self.base * 2**retry)
        jittered = self.rng.uniform(0, bound)
        return max(jittered, retry_after or 0.0)


DEFAULT_POLICY = RetryPolicy()


def retrying(
    operation: Callable[[], T],
    *,
    what: str,
    policy: RetryPolicy | None = None,
    before_retry: Callable[[], T | None] | None = None,
) -> T:
    """Run ``operation``, retrying it while it raises :class:`TransientError`.

    ``before_retry`` runs before each new try. If it returns something other than ``None``, that
    is returned instead of trying again: connectors use it to find their own write when an
    earlier try may have succeeded without the answer arriving.

    Raises :class:`ConnectorError` once the attempts or the waiting time run out.
    """
    policy = policy or DEFAULT_POLICY
    waited = 0.0
    for retry in range(policy.attempts):
        try:
            return operation()
        except TransientError as exc:
            last = exc
            if retry + 1 >= policy.attempts:
                break
            delay = policy.backoff(retry, exc.retry_after)
            if waited + delay > policy.max_wait:
                break
            policy.sleep(delay)
            waited += delay
        if before_retry is not None:
            found = before_retry()
            if found is not None:
                return found
    raise ConnectorError(f"{what} kept failing; try again later ({last})") from last


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header: delay-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())
