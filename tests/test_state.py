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
from src.state.models import (
    LEGACY_STATE_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    DatabaseState,
    ExecutionState,
    SchemaState,
)

MOMENT = datetime(2026, 1, 1, 12, 30, 45, tzinfo=UTC)


def _populated() -> ExecutionState:
    return ExecutionState(
        databases={
            "HRDB": DatabaseState(
                database_name="HRDB",
                schemas={
                    "HR": SchemaState(
                        schema_name="HR",
                        last_successful_run=MOMENT,
                        commit_sha="abc123",
                        execution_status=ExecutionStatus.SUCCESS,
                        processed_objects=7,
                        execution_duration_seconds=12.5,
                    )
                },
            )
        }
    )


# --------------------------------------------------------------------------- model


def test_missing_entry_means_full_export() -> None:
    """No watermark for a schema implies a full export of it."""
    assert ExecutionState().watermark_for("TESTDB", "HR") is None
    assert _populated().watermark_for("OTHERDB", "HR") is None


def test_watermark_is_returned_when_present() -> None:
    """A recorded last successful run becomes that schema's watermark."""
    assert _populated().watermark_for("HRDB", "HR") == MOMENT


def test_a_new_schema_of_a_known_database_has_no_watermark() -> None:
    """Adding a schema is self-correcting: it gets a full export, not a stale one.

    The defect this prevents: under the per-database model, a schema added to
    the include filter inherited the database's watermark and silently skipped
    everything that had changed in it beforehand — while reconciliation, which
    is never incremental, applied to it in full.
    """
    assert _populated().watermark_for("HRDB", "PAYROLL") is None


def test_default_schema_version() -> None:
    """The schema version is frozen; bumping it needs owner approval."""
    assert ExecutionState().schema_version == STATE_SCHEMA_VERSION == 2


def test_schema_state_defaults() -> None:
    """A fresh schema state carries no watermark and no commit."""
    state = SchemaState(schema_name="HR")
    assert state.last_successful_run is None
    assert state.commit_sha is None
    assert state.execution_status is ExecutionStatus.SUCCESS
    assert state.processed_objects == 0


def test_with_schema_does_not_mutate_the_original() -> None:
    """Advancing a watermark produces a new value, never an in-place edit."""
    original = ExecutionState()
    updated = original.with_schema("HRDB", SchemaState("HR", last_successful_run=MOMENT))
    assert original.databases == {}
    assert updated.watermark_for("HRDB", "HR") == MOMENT


def test_advancing_one_schema_leaves_its_siblings_alone() -> None:
    """A schema's watermark is its own; nothing else in the run may move it."""
    later = MOMENT + timedelta(hours=1)
    state = _populated().with_schema("HRDB", SchemaState("PAYROLL", last_successful_run=later))
    assert state.watermark_for("HRDB", "HR") == MOMENT
    assert state.watermark_for("HRDB", "PAYROLL") == later


# --------------------------------------------------------------------------- serialization


def test_round_trip_preserves_every_field() -> None:
    """Serializing and deserializing is lossless."""
    state = _populated()
    assert ExecutionState.from_dict(state.to_dict()) == state


def test_timestamps_serialize_as_utc_iso8601() -> None:
    """Serialized timestamps never depend on the runner's timezone."""
    offset = datetime(2026, 1, 1, 14, 30, 45, tzinfo=timezone(timedelta(hours=2)))
    payload = SchemaState("HR", last_successful_run=offset).to_dict()
    assert payload["last_successful_run"] == "2026-01-01T12:30:45+00:00"


def test_naive_timestamps_are_read_as_utc() -> None:
    """A naive timestamp is UTC, never local time."""
    state = SchemaState.from_dict(
        {"schema_name": "HR", "last_successful_run": "2026-01-01T12:30:45"}, "ctx"
    )
    assert state.last_successful_run == MOMENT


def test_databases_serialize_in_sorted_order() -> None:
    """A stable key order keeps the state file free of spurious Git diffs."""
    state = ExecutionState(
        databases={n: DatabaseState(n) for n in ("ZDB", "ADB", "MDB")}
    )
    assert list(state.to_dict()["databases"]) == ["ADB", "MDB", "ZDB"]


def test_schemas_serialize_in_sorted_order() -> None:
    """Same reason, one level down."""
    state = ExecutionState()
    for name in ("ZS", "AS_", "MS"):
        state = state.with_schema("HRDB", SchemaState(name))
    serialized = state.to_dict()["databases"]["HRDB"]["schemas"]
    assert list(serialized) == ["AS_", "MS", "ZS"]


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
        ({"databases": {"HRDB": {"database_name": "HRDB", "schemas": []}}}, "expected an object"),
        (
            {"databases": {"HRDB": {"database_name": "HRDB", "schemas": {"HR": {}}}}},
            "non-empty string",
        ),
        (
            {
                "databases": {
                    "HRDB": {
                        "database_name": "HRDB",
                        "schemas": {"HR": {"schema_name": "OTHER"}},
                    }
                }
            },
            "contradicts its key",
        ),
        (
            {
                "databases": {
                    "HRDB": {
                        "database_name": "HRDB",
                        "schemas": {"HR": {"schema_name": "HR", "last_successful_run": "junk"}},
                    }
                }
            },
            "not a valid ISO-8601",
        ),
        (
            {
                "databases": {
                    "HRDB": {
                        "database_name": "HRDB",
                        "schemas": {"HR": {"schema_name": "HR", "processed_objects": -1}},
                    }
                }
            },
            "non-negative integer",
        ),
        (
            {
                "databases": {
                    "HRDB": {
                        "database_name": "HRDB",
                        "schemas": {"HR": {"schema_name": "HR", "execution_status": "WAT"}},
                    }
                }
            },
            "unknown status",
        ),
    ],
)
def test_malformed_state_is_rejected(payload: object, match: str) -> None:
    """Every structural defect raises StateError rather than being tolerated."""
    with pytest.raises(StateError, match=match):
        ExecutionState.from_dict(payload)


# --------------------------------------------------------------------------- migration


def _v1_payload() -> dict:
    """A state file as the per-database build wrote it."""
    return {
        "schema_version": LEGACY_STATE_SCHEMA_VERSION,
        "databases": {
            "HRDB": {
                "database_name": "HRDB",
                "last_successful_run": MOMENT.isoformat(),
                "commit_sha": "abc123",
                "execution_status": "SUCCESS",
                "processed_objects": 3407,
                "execution_duration_seconds": 153.0,
            }
        },
    }


def test_v1_state_is_migrated_not_rejected() -> None:
    """An existing deployment's state file still loads."""
    state = ExecutionState.from_dict(_v1_payload())
    assert state.schema_version == STATE_SCHEMA_VERSION


def test_v1_watermarks_are_discarded() -> None:
    """Decision D-18: no schema inherits a watermark it cannot be proven to own.

    A v1 file records one watermark per database and does not record which
    schemas were in scope when it was written. Attributing it to whichever
    schemas happen to be configured now would silently skip every change made
    to a schema added since — the same class of undetectable data loss the
    timezone defect produced.
    """
    state = ExecutionState.from_dict(_v1_payload())
    assert state.watermark_for("HRDB", "HR") is None
    assert state.databases == {}


def test_migrated_state_is_written_back_at_the_new_version(tmp_path: Path) -> None:
    """The upgrade is durable: the next run reads v2, not v1 again."""
    path = tmp_path / "execution_state.json"
    path.write_text(json.dumps(_v1_payload()), encoding="utf-8")
    store = ExecutionStateStore(path)
    migrated = store.load()
    store.save(migrated.with_schema("HRDB", SchemaState("HR", last_successful_run=MOMENT)))

    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["schema_version"] == STATE_SCHEMA_VERSION
    assert reloaded["databases"]["HRDB"]["schemas"]["HR"]["last_successful_run"]


# --------------------------------------------------------------------------- store


def test_missing_file_means_full_export(tmp_path: Path) -> None:
    """No state file is a normal first run, not an error."""
    state = ExecutionStateStore(tmp_path / "execution_state.json").load()
    assert state.databases == {}
    assert state.watermark_for("HRDB", "HR") is None


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
    """The state/execution_state.json in this repository is valid.

    Deliberately *not* asserted empty. Every successful run advances this
    file and commits it, so an emptiness assertion holds only until the
    agent runs once — after which it fails the very pipeline that produced
    it. What must hold is that the committed file still parses at the
    current schema version and that each entry is keyed by its own database
    name, since a mismatch there would read a watermark back for the wrong
    database and silently skip changes.
    """
    state = ExecutionStateStore(project_root / "state" / "execution_state.json").load()
    assert state.schema_version == STATE_SCHEMA_VERSION
    assert all(name == db.database_name for name, db in state.databases.items())
