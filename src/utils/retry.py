"""Retry with exponential backoff (docs/architecture.md §15).

Retries apply **only** to failures that are plausibly transient. A
deterministic failure is never retried: retrying it wastes time and multiplies
log noise without any chance of a different outcome.

The clock and the random source are injected, so backoff sequencing is asserted
exactly in tests rather than by sleeping.

**This module is not in the approved structure** (CLAUDE.md §3) — the retry
policy is specified in architecture §15 but has no module entry in §2. It needs
owner ratification; see the Phase 3 report.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["RetryPolicy", "retry_call"]



@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times to retry, and how long to wait between attempts."""

    #: Total attempts, including the first. ``1`` means "never retry".
    attempts: int = 3
    #: Delay before the first retry; doubled for each subsequent one.
    base_delay: float = 2.0
    #: Fraction of the delay applied as random jitter, spreading out retries
    #: when several run units fail against the same listener at once.
    jitter_ratio: float = 0.2
    #: Overall wall-clock budget; no further retry starts once it is spent.
    max_total_seconds: float = 300.0

    def delay_for(self, attempt: int, jitter: float = 0.0) -> float:
        """Return the delay before retry number ``attempt``.

        Args:
            attempt: 1 for the first retry, 2 for the second, and so on.
            jitter: A value in ``[0, 1)``, normally from a random source.

        Returns:
            The delay in seconds: ``base_delay * 2 ** (attempt - 1)`` scaled by
            ``1 ± jitter_ratio``.
        """
        base = self.base_delay * (2 ** (attempt - 1))
        return base * (1 + self.jitter_ratio * (2 * jitter - 1))


def retry_call[T](
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
    is_retryable: Callable[[Exception], bool],
    description: str = "operation",
    on_retry: Callable[[int, float, Exception], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter_source: Callable[[], float] = random.random,
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    """Call ``operation``, retrying only failures ``is_retryable`` accepts.

    Args:
        operation: The zero-argument callable to run.
        policy: Attempt count, backoff, and overall budget.
        is_retryable: Decides whether a given exception is worth retrying. A
            deterministic failure must return ``False`` here.
        description: Used in the ``on_retry`` notification.
        on_retry: Called with ``(attempt, delay, exception)`` before each wait,
            so the caller can log without this module importing the logger.
        sleep: Injected for testing.
        jitter_source: Injected for testing; returns a value in ``[0, 1)``.
        monotonic: Injected for testing; used for the overall budget.

    Returns:
        Whatever ``operation`` returns.

    Raises:
        Exception: The last exception raised by ``operation``, once the
            attempts or the time budget are exhausted, or immediately if
            ``is_retryable`` rejects it.
    """
    started = monotonic()
    last: Exception

    for attempt in range(1, max(1, policy.attempts) + 1):
        try:
            return operation()
        except Exception as exc:
            last = exc
            if attempt >= policy.attempts or not is_retryable(exc):
                raise

            delay = policy.delay_for(attempt, jitter_source())
            if monotonic() - started + delay > policy.max_total_seconds:
                raise
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleep(delay)

    raise last  # pragma: no cover - unreachable; the loop always returns or raises
