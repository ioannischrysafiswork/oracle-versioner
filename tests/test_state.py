"""Execution state: round-trip, strict validation, and the write rules.

Two rules dominate (docs/architecture.md §14.3):

  * a missing file means a full export;
  * a corrupt file is fatal and is **never** recreated, because a silent
    recreation would trigger a full export that masks data loss.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.exceptions import StateError
from src.models.execution import ExecutionStatus
from src.state.execution_state import ExecutionStateStore
from src.state.models import STATE_SCHEMA_VERSION, DatabaseState, ExecutionState

MOMENT = datetime(2026, 1, 1, 12, 30, 45, tzinfo=UTC)


def _populated() -> ExecutionState:
    return ExecutionState(
        databases={
            "HRDB": DatabaseState(
                database_name="HRDB",
                last_successful_run=MOMENT,
                commit_sha="abc123",
                execution_status=ExecutionStatus.SUCCESS,
                processed_objects=7,
                execution_duration_seconds=12.5,
            )
        }
    )


# --------------------------------------------------------------------------- model


def test_missing_database_entry_means_full_export() -> None:
    """No watermark for a database implies a full export."""
    assert ExecutionState().watermark_for("TESTDB") is None
    assert _populated().watermark_for("OTHERDB") is None


def test_watermark_is_returned_when_present() -> None:
    """A recorded last successful run becomes the watermark."""
    assert _populated().watermark_for("HRDB") == MOMENT


def test_default_schema_version() -> None:
    """The schema version is frozen; bumping it needs owner approval."""
    assert ExecutionState().schema_version == STATE_SCHEMA_VERSION == 1


def test_database_state_defaults() -> None:
    """A fresh database state carries no watermark and no commit."""
    state = DatabaseState(database_name="TESTDB")
    assert state.last_successful_run is None
    assert state.commit_sha is None
    assert state.execution_status is ExecutionStatus.SUCCESS
    assert state.processed_objects == 0


def test_with_database_does_not_mutate_the_original() -> None:
    """Advancing a watermark produces a new value, never an in-place edit."""
    original = ExecutionState()
    updated = original.with_database(DatabaseState("HRDB", last_successful_run=MOMENT))
    assert original.databases == {}
    assert updated.watermark_for("HRDB") == MOMENT


# --------------------------------------------------------------------------- serialization


def test_round_trip_preserves_every_field() -> None:
    """Serializing and deserializing is lossless."""
    state = _populated()
    assert ExecutionState.from_dict(state.to_dict()) == state


def test_timestamps_serialize_as_utc_iso8601() -> None:
    """Serialized timestamps never depend on the runner's timezone."""
    offset = datetime(2026, 1, 1, 14, 30, 45, tzinfo=timezone(timedelta(hours=2)))
    payload = DatabaseState("HRDB", last_successful_run=offset).to_dict()
    assert payload["last_successful_run"] == "2026-01-01T12:30:45+00:00"


def test_naive_timestamps_are_read_as_utc() -> None:
    """A naive timestamp is UTC, never local time."""
    state = DatabaseState.from_dict(
        {"database_name": "HRDB", "last_successful_run": "2026-01-01T12:30:45"}, "ctx"
    )
    assert state.last_successful_run == MOMENT


def test_databases_serialize_in_sorted_order() -> None:
    """A stable key order keeps the state file free of spurious Git diffs."""
    state = ExecutionState(
        databases={n: DatabaseState(n) for n in ("ZDB", "ADB", "MDB")}
    )
    assert list(state.to_dict()["databases"]) == ["ADB", "MDB", "ZDB"]


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ([], "expected an object"),
        ({"unexpected": 1}, "unknown key"),
        ({"schema_version": 99}, "unsupported schema_version"),
        ({"databases": []}, "expected an object"),
        ({"databases": {"HRDB": {"database_name": "OTHER"}}}, "contradicts its key"),
        ({"databases": {"HRDB": {"database_name": "HRDB", "nope": 1}}}, "unknown key"),
        ({"databases": {"HRDB": {"database_name": ""}}}, "non-empty string"),
        (
            {"databases": {"HRDB": {"database_name": "HRDB", "last_successful_run": "junk"}}},
            "not a valid ISO-8601",
        ),
        (
            {"databases": {"HRDB": {"database_name": "HRDB", "processed_objects": -1}}},
            "non-negative integer",
        ),
        (
            {"databases": {"HRDB": {"database_name": "HRDB", "execution_status": "WAT"}}},
            "unknown status",
        ),
    ],
)
def test_malformed_state_is_rejected(payload: object, match: str) -> None:
    """Every structural defect raises StateError rather than being tolerated."""
    with pytest.raises(StateError, match=match):
        ExecutionState.from_dict(payload)


# --------------------------------------------------------------------------- store


def test_missing_file_means_full_export(tmp_path: Path) -> None:
    """No state file is a normal first run, not an error."""
    state = ExecutionStateStore(tmp_path / "execution_state.json").load()
    assert state.databases == {}
    assert state.watermark_for("HRDB") is None


def test_corrupt_file_is_fatal_and_left_untouched(tmp_path: Path) -> None:
    """A corrupt state file is never silently recreated."""
    path = tmp_path / "execution_state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError, match="not valid JSON"):
        ExecutionStateStore(path).load()
    assert path.read_text(encoding="utf-8") == "{not json"


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    """State survives a write/read cycle unchanged."""
    store = ExecutionStateStore(tmp_path / "execution_state.json")
    store.save(_populated())
    assert store.load() == _populated()


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    """A fresh checkout without state/ still works."""
    store = ExecutionStateStore(tmp_path / "state" / "execution_state.json")
    store.save(ExecutionState())
    assert store.path.exists()


def test_save_is_atomic_and_leaves_no_temporary_files(tmp_path: Path) -> None:
    """An interrupted write can never leave a partial state file behind."""
    store = ExecutionStateStore(tmp_path / "execution_state.json")
    store.save(_populated())
    assert [p.name for p in tmp_path.iterdir()] == ["execution_state.json"]


def test_save_output_is_lf_terminated_utf8(tmp_path: Path) -> None:
    """No CRLF, and exactly one trailing newline."""
    store = ExecutionStateStore(tmp_path / "execution_state.json")
    store.save(_populated())
    raw = store.path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"}\n")
    assert json.loads(raw.decode("utf-8"))["schema_version"] == STATE_SCHEMA_VERSION


def test_store_records_whether_it_saved(tmp_path: Path) -> None:
    """`saved` makes "state is never written before the push" testable."""
    store = ExecutionStateStore(tmp_path / "execution_state.json")
    assert store.saved is False
    store.load()
    assert store.saved is False
    store.save(ExecutionState())
    assert store.saved is True


def test_committed_state_file_parses(project_root: Path) -> None:
    """The state/execution_state.json in this repository is valid."""
    state = ExecutionStateStore(project_root / "state" / "execution_state.json").load()
    assert state.schema_version == STATE_SCHEMA_VERSION
    assert state.databases == {}
