"""Retry policy (docs/architecture.md §15), with a controlled clock.

Sleeping is injected, so backoff sequencing is asserted exactly rather than
waited for. The distinction that matters most: a *deterministic* failure is
never retried, because retrying it only wastes time and multiplies log noise.
"""

from __future__ import annotations

import pytest

from src.utils.retry import RetryPolicy, retry_call


class Clock:
    """Records requested sleeps and advances a fake monotonic clock."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _always_retry(_exc: Exception) -> bool:
    return True


def _never_retry(_exc: Exception) -> bool:
    return False


def test_successful_call_does_not_retry() -> None:
    """A call that works is made exactly once."""
    calls: list[int] = []
    result = retry_call(
        lambda: (calls.append(1), "ok")[1],
        policy=RetryPolicy(attempts=3),
        is_retryable=_always_retry,
        sleep=lambda _s: None,
    )
    assert result == "ok"
    assert len(calls) == 1


def test_transient_failure_is_retried_until_it_succeeds() -> None:
    """A recoverable failure is retried within the budget."""
    attempts: list[int] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("listener restarting")
        return "ok"

    clock = Clock()
    result = retry_call(
        flaky,
        policy=RetryPolicy(attempts=3, base_delay=2.0, jitter_ratio=0.0),
        is_retryable=_always_retry,
        sleep=clock.sleep,
        jitter_source=lambda: 0.5,
        monotonic=clock.monotonic,
    )
    assert result == "ok"
    assert len(attempts) == 3


def test_backoff_is_exponential() -> None:
    """2s, then 4s, then 8s — the sequence docs/architecture.md §15 specifies."""
    clock = Clock()
    with pytest.raises(OSError):
        retry_call(
            lambda: (_ for _ in ()).throw(OSError("down")),
            policy=RetryPolicy(attempts=4, base_delay=2.0, jitter_ratio=0.0),
            is_retryable=_always_retry,
            sleep=clock.sleep,
            jitter_source=lambda: 0.5,
            monotonic=clock.monotonic,
        )
    assert clock.slept == [2.0, 4.0, 8.0]


@pytest.mark.parametrize(
    ("jitter", "expected"), [(0.0, 1.6), (0.5, 2.0), (1.0, 2.4)]
)
def test_jitter_spreads_retries(jitter: float, expected: float) -> None:
    """±20% jitter stops several run units retrying in lockstep."""
    policy = RetryPolicy(base_delay=2.0, jitter_ratio=0.2)
    assert policy.delay_for(1, jitter) == pytest.approx(expected)


def test_deterministic_failure_is_never_retried() -> None:
    """A missing privilege fails identically every time; do not retry it."""
    attempts: list[int] = []

    def failing() -> None:
        attempts.append(1)
        raise PermissionError("ORA-01031")

    clock = Clock()
    with pytest.raises(PermissionError):
        retry_call(
            failing,
            policy=RetryPolicy(attempts=5),
            is_retryable=_never_retry,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
    assert len(attempts) == 1
    assert clock.slept == []


def test_attempts_of_one_means_no_retry() -> None:
    """A policy of one attempt disables retrying entirely."""
    attempts: list[int] = []

    def failing() -> None:
        attempts.append(1)
        raise OSError("down")

    with pytest.raises(OSError):
        retry_call(
            failing,
            policy=RetryPolicy(attempts=1),
            is_retryable=_always_retry,
            sleep=lambda _s: None,
        )
    assert len(attempts) == 1


def test_time_budget_stops_further_retries() -> None:
    """No execution can hang the pipeline until the runner timeout."""
    clock = Clock()
    with pytest.raises(OSError):
        retry_call(
            lambda: (_ for _ in ()).throw(OSError("down")),
            policy=RetryPolicy(
                attempts=10, base_delay=2.0, jitter_ratio=0.0, max_total_seconds=7.0
            ),
            is_retryable=_always_retry,
            sleep=clock.sleep,
            jitter_source=lambda: 0.5,
            monotonic=clock.monotonic,
        )
    assert clock.slept == [2.0, 4.0]  # the next delay of 8s would breach the budget


def test_on_retry_is_notified_before_each_wait() -> None:
    """The caller can log retries without this module importing the logger."""
    notifications: list[tuple[int, float]] = []
    with pytest.raises(OSError):
        retry_call(
            lambda: (_ for _ in ()).throw(OSError("down")),
            policy=RetryPolicy(attempts=3, base_delay=1.0, jitter_ratio=0.0),
            is_retryable=_always_retry,
            on_retry=lambda attempt, delay, _exc: notifications.append((attempt, delay)),
            sleep=lambda _s: None,
            jitter_source=lambda: 0.5,
        )
    assert notifications == [(1, 1.0), (2, 2.0)]
