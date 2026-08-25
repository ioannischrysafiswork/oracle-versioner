"""Duration measurement and UTC timestamp helpers.

Timestamps in state and logs are timezone-aware UTC (CLAUDE.md §7.3). Note the
distinction: these helpers use the *runner* clock, which is fine for durations
and log records. Watermarks must come from the *database* clock and are
therefore never produced here (docs/architecture.md §4.2).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from types import TracebackType

__all__ = ["Timer", "format_duration", "parse_utc_offset", "to_utc", "utc_now"]


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Returns:
        The runner's current UTC time.
    """
    return datetime.now(UTC)


def parse_utc_offset(text: str) -> timezone:
    """Parse an Oracle ``TZH:TZM`` offset such as ``+03:00`` or ``-05:30``.

    Args:
        text: The offset as ``TO_CHAR(SYSTIMESTAMP, 'TZH:TZM')`` renders it.

    Returns:
        The corresponding fixed-offset timezone.

    Raises:
        ValueError: If the text is not a valid signed ``HH:MM`` offset.
    """
    cleaned = text.strip()
    sign = -1 if cleaned.startswith("-") else 1
    hours, _, minutes = cleaned.lstrip("+-").partition(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes or 0)))


def to_utc(moment: datetime) -> datetime:
    """Normalize ``moment`` to timezone-aware UTC.

    A naive datetime is interpreted as UTC rather than as local time: local
    time would make serialized state depend on the runner's timezone.

    Args:
        moment: Any datetime.

    Returns:
        The equivalent timezone-aware UTC datetime.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def format_duration(seconds: float) -> str:
    """Render a duration for a log record or commit body.

    Args:
        seconds: Elapsed wall-clock seconds. Negative input is clamped to zero.

    Returns:
        ``4.2s``, ``2m 03s``, or ``1h 02m 03s`` depending on magnitude.
    """
    total = max(0.0, float(seconds))
    if total < 60:
        return f"{total:.1f}s"

    whole = int(total)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


class Timer:
    """Context manager measuring elapsed wall-clock time.

    Uses a monotonic clock, so a system clock adjustment mid-run cannot produce
    a negative or wildly wrong duration.
    """

    def __init__(self) -> None:
        """Create an unstarted timer."""
        self._start: float | None = None
        self.elapsed: float = 0.0

    def __enter__(self) -> Timer:
        """Start measuring.

        Returns:
            This timer.
        """
        self._start = time.monotonic()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop measuring, recording the result in :attr:`elapsed`.

        Args:
            exc_type: Exception class, if the block raised.
            exc: Exception instance, if the block raised.
            traceback: Traceback, if the block raised.
        """
        if self._start is not None:
            self.elapsed = time.monotonic() - self._start
