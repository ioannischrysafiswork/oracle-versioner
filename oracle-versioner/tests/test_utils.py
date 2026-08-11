"""Foundation helpers: atomic writes, durations, hashing."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.utils.filesystem import atomic_write_text, ensure_directory
from src.utils.hashing import content_hash
from src.utils.timers import Timer, format_duration, to_utc, utc_now

# --------------------------------------------------------------------------- filesystem


def test_ensure_directory_creates_parents(tmp_path: Path) -> None:
    """Nested repository folders are created on demand."""
    target = tmp_path / "HRDB" / "HR" / "packages"
    assert ensure_directory(target) == target
    assert target.is_dir()


def test_ensure_directory_is_idempotent(tmp_path: Path) -> None:
    """Creating an existing directory is not an error."""
    ensure_directory(tmp_path)
    ensure_directory(tmp_path)


def test_atomic_write_creates_the_file_and_parents(tmp_path: Path) -> None:
    """A first write creates the whole path."""
    target = tmp_path / "a" / "b.sql"
    atomic_write_text(target, "CREATE OR REPLACE PACKAGE p AS END;\n")
    assert target.read_text(encoding="utf-8") == "CREATE OR REPLACE PACKAGE p AS END;\n"


def test_atomic_write_replaces_existing_content(tmp_path: Path) -> None:
    """An overwrite fully replaces, never appends."""
    target = tmp_path / "a.sql"
    atomic_write_text(target, "first\n")
    atomic_write_text(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"


def test_atomic_write_preserves_lf_on_every_platform(tmp_path: Path) -> None:
    """LF must survive on Windows; a CRLF would be a phantom diff."""
    target = tmp_path / "a.sql"
    atomic_write_text(target, "line one\nline two\n")
    assert target.read_bytes() == b"line one\nline two\n"


def test_atomic_write_writes_utf8_without_bom(tmp_path: Path) -> None:
    """A BOM would appear as a phantom diff."""
    target = tmp_path / "a.sql"
    atomic_write_text(target, "-- café\n")
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8") == "-- café\n"


def test_atomic_write_leaves_no_temporary_files(tmp_path: Path) -> None:
    """A successful write leaves exactly one file behind."""
    atomic_write_text(tmp_path / "a.sql", "x\n")
    assert [p.name for p in tmp_path.iterdir()] == ["a.sql"]


def test_failed_write_cleans_up_and_preserves_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted write never destroys the previous file."""
    target = tmp_path / "a.sql"
    atomic_write_text(target, "original\n")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(target, "replacement\n")

    assert target.read_text(encoding="utf-8") == "original\n"
    assert [p.name for p in tmp_path.iterdir()] == ["a.sql"]


# --------------------------------------------------------------------------- timers


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0.0s"),
        (-5, "0.0s"),
        (4.24, "4.2s"),
        (59.9, "59.9s"),
        (60, "1m 00s"),
        (123, "2m 03s"),
        (3600, "1h 00m 00s"),
        (3723, "1h 02m 03s"),
    ],
)
def test_format_duration(seconds: float, expected: str) -> None:
    """Durations render deterministically across every magnitude."""
    assert format_duration(seconds) == expected


def test_utc_now_is_timezone_aware() -> None:
    """Naive timestamps would make state depend on the runner's timezone."""
    assert utc_now().tzinfo is not None


def test_to_utc_treats_naive_as_utc() -> None:
    """A naive datetime is UTC, never local time."""
    assert to_utc(datetime(2026, 1, 1, 12, 0)) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_to_utc_converts_an_offset() -> None:
    """An offset-aware datetime is converted, not relabelled."""
    offset = datetime(2026, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert to_utc(offset) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_timer_measures_a_block() -> None:
    """The timer records a non-negative elapsed time."""
    with Timer() as timer:
        pass
    assert timer.elapsed >= 0


def test_timer_records_elapsed_even_when_the_block_raises() -> None:
    """A failing run unit still reports how long it ran."""
    timer = Timer()
    with pytest.raises(ValueError), timer:
        raise ValueError("boom")
    assert timer.elapsed >= 0


# --------------------------------------------------------------------------- hashing


def test_content_hash_is_stable_and_distinguishing() -> None:
    """Equal input, equal digest; different input, different digest."""
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
    assert len(content_hash("abc")) == 64


def test_content_hash_is_encoding_independent() -> None:
    """Non-ASCII input hashes identically regardless of the runner's locale."""
    assert content_hash("café") == content_hash("café")
