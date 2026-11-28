import random
from datetime import UTC, datetime

import pytest

from usmp.sdk.connector import ConnectorError
from usmp.sdk.retry import RetryPolicy, TransientError, parse_retry_after, retrying


def policy(**overrides: object) -> tuple[RetryPolicy, list[float]]:
    slept: list[float] = []
    rng = random.Random(1)  # noqa: S311 - jitter, not security
    settings: dict[str, object] = {"sleep": slept.append, "rng": rng, **overrides}
    return RetryPolicy(**settings), slept  # type: ignore[arg-type]


def flaky(failures: list[TransientError], result: str = "ok") -> object:
    def attempt() -> str:
        if failures:
            raise failures.pop(0)
        return result

    return attempt


def test_succeeds_after_transient_failures() -> None:
    p, slept = policy()
    attempt = flaky([TransientError("busy"), TransientError("busy")])

    assert retrying(attempt, what="x", policy=p) == "ok"  # type: ignore[arg-type]
    assert len(slept) == 2


def test_backoff_has_full_jitter_and_doubles_its_bound() -> None:
    p, _ = policy(base=2.0, cap=10.0)
    waits = [[p.backoff(retry, None) for _ in range(200)] for retry in range(5)]

    for retry, samples in enumerate(waits):
        bound = min(10.0, 2.0 * 2**retry)
        assert all(0 <= w <= bound for w in samples)
        assert max(samples) > bound * 0.8, "uses the whole range"
        assert min(samples) < bound * 0.2


def test_retry_after_is_a_minimum() -> None:
    p, slept = policy(base=0.01)
    attempt = flaky([TransientError("slow down", retry_after=7)])

    retrying(attempt, what="x", policy=p)  # type: ignore[arg-type]

    assert slept == [7]


def test_gives_up_after_the_attempts() -> None:
    p, slept = policy(attempts=3)
    attempt = flaky([TransientError(f"busy {n}") for n in range(5)])

    with pytest.raises(ConnectorError, match=r"saving kept failing; try again later \(busy 2\)"):
        retrying(attempt, what="saving", policy=p)  # type: ignore[arg-type]
    assert len(slept) == 2


def test_gives_up_when_waiting_would_take_too_long() -> None:
    p, slept = policy(max_wait=10)
    attempt = flaky([TransientError("busy", retry_after=6), TransientError("busy", retry_after=6)])

    with pytest.raises(ConnectorError, match="kept failing"):
        retrying(attempt, what="x", policy=p)  # type: ignore[arg-type]
    assert slept == [6]


def test_other_errors_are_not_retried() -> None:
    p, slept = policy()

    def attempt() -> str:
        raise ConnectorError("bad password")

    with pytest.raises(ConnectorError, match="bad password"):
        retrying(attempt, what="x", policy=p, before_retry=lambda: pytest.fail("no retry"))
    assert slept == []


def test_before_retry_can_find_an_earlier_write() -> None:
    p, _ = policy()
    tries: list[int] = []

    def attempt() -> str:
        tries.append(1)
        raise TransientError("connection dropped")

    assert retrying(attempt, what="x", policy=p, before_retry=lambda: "found") == "found"
    assert len(tries) == 1


def test_before_retry_returning_none_tries_again() -> None:
    p, _ = policy()
    attempt = flaky([TransientError("dropped")])

    assert retrying(attempt, what="x", policy=p, before_retry=lambda: None) == "ok"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        (None, None),
        ("", None),
        ("120", 120.0),
        (" 3 ", 3.0),
        ("Wed, 21 Oct 2026 07:28:30 GMT", 30.0),
        ("Wed, 21 Oct 2026 07:27:00 GMT", 0.0),
        ("soon", None),
    ],
)
def test_parse_retry_after(header: str | None, seconds: float | None) -> None:
    now = datetime(2026, 10, 21, 7, 28, tzinfo=UTC)
    assert parse_retry_after(header, now=now) == seconds


def test_parse_retry_after_without_zone_is_utc() -> None:
    now = datetime(2026, 10, 21, 7, 28, tzinfo=UTC)
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:10 -0000", now=now) == 10.0
