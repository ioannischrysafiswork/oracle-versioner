"""``execution_state.json`` data model and (de)serialization.

See docs/architecture.md §2 and docs/requirements.md §4.

Each database maintains its own state, because databases are processed
independently and a failure in one must not advance another's watermark
(docs/requirements.md §23).

A database with no entry here is treated exactly like a missing state file:
**full export**. That is deliberate — there is no other safe interpretation of
"we have never successfully synchronized this database".

Deserialization is strict. Anything unexpected raises
:class:`~src.exceptions.StateError`, which is execution-fatal: silently
tolerating a malformed state file would produce a full export that looks like
success while masking data loss.

The schema is frozen: changing it requires owner approval (CLAUDE.md §8.1).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.exceptions import StateError
from src.models.execution import ExecutionStatus
from src.utils.timers import to_utc

__all__ = ["STATE_SCHEMA_VERSION", "DatabaseState", "ExecutionState"]

#: Bumping this is a schema change and needs owner approval.
STATE_SCHEMA_VERSION = 1


def _parse_timestamp(value: Any, context: str) -> datetime | None:
    """Parse an ISO-8601 timestamp from the state file.

    Args:
        value: The raw JSON value; ``None`` is permitted.
        context: Field path, used in the error message.

    Returns:
        A timezone-aware UTC datetime, or ``None``.

    Raises:
        StateError: If the value is neither ``None`` nor a valid timestamp.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError(f"{context}: expected an ISO-8601 string, got {type(value).__name__}")
    try:
        return to_utc(datetime.fromisoformat(value))
    except ValueError as exc:
        raise StateError(f"{context}: not a valid ISO-8601 timestamp: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class DatabaseState:
    """The last successful synchronization of one database."""

    database_name: str
    last_successful_run: datetime | None = None
    commit_sha: str | None = None
    execution_status: ExecutionStatus = ExecutionStatus.SUCCESS
    processed_objects: int = 0
    execution_duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible mapping.

        Returns:
            The mapping, with the timestamp as an ISO-8601 UTC string.
        """
        return {
            "database_name": self.database_name,
            "last_successful_run": (
                to_utc(self.last_successful_run).isoformat()
                if self.last_successful_run
                else None
            ),
            "commit_sha": self.commit_sha,
            "execution_status": str(self.execution_status),
            "processed_objects": self.processed_objects,
            "execution_duration_seconds": self.execution_duration_seconds,
        }

    @classmethod
    def from_dict(cls, data: Any, context: str) -> DatabaseState:
        """Deserialize from a mapping, validating strictly.

        Args:
            data: The raw JSON value.
            context: Field path, used in error messages.

        Returns:
            The parsed state.

        Raises:
            StateError: On any structural or type error.
        """
        if not isinstance(data, dict):
            raise StateError(f"{context}: expected an object, got {type(data).__name__}")

        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(data) - allowed
        if unknown:
            raise StateError(f"{context}: unknown key(s) {sorted(unknown)}")

        name = data.get("database_name")
        if not isinstance(name, str) or not name:
            raise StateError(f"{context}.database_name: expected a non-empty string")

        status = data.get("execution_status", ExecutionStatus.SUCCESS)
        try:
            status = ExecutionStatus(status)
        except ValueError as exc:
            raise StateError(f"{context}.execution_status: unknown status {status!r}") from exc

        processed = data.get("processed_objects", 0)
        if not isinstance(processed, int) or isinstance(processed, bool) or processed < 0:
            raise StateError(f"{context}.processed_objects: expected a non-negative integer")

        duration = data.get("execution_duration_seconds", 0.0)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
            raise StateError(f"{context}.execution_duration_seconds: expected a non-negative number")

        sha = data.get("commit_sha")
        if sha is not None and not isinstance(sha, str):
            raise StateError(f"{context}.commit_sha: expected a string or null")

        return cls(
            database_name=name,
            last_successful_run=_parse_timestamp(
                data.get("last_successful_run"), f"{context}.last_successful_run"
            ),
            commit_sha=sha,
            execution_status=status,
            processed_objects=processed,
            execution_duration_seconds=float(duration),
        )


@dataclass(frozen=True, slots=True)
class ExecutionState:
    """The whole state file: one :class:`DatabaseState` per database."""

    schema_version: int = STATE_SCHEMA_VERSION
    databases: dict[str, DatabaseState] = field(default_factory=dict)

    def watermark_for(self, database_name: str) -> datetime | None:
        """Return the watermark for ``database_name``.

        Args:
            database_name: The configured database name.

        Returns:
            The last successful run timestamp, or ``None`` when this database
            has never completed a synchronization — meaning a full export.
        """
        state = self.databases.get(database_name)
        return state.last_successful_run if state else None

    def with_database(self, state: DatabaseState) -> ExecutionState:
        """Return a copy with ``state`` replacing that database's entry.

        The state object is frozen, so advancing a watermark produces a new
        value rather than mutating the one already loaded — which keeps "never
        update state before the push succeeds" easy to verify.

        Args:
            state: The database state to record.

        Returns:
            A new :class:`ExecutionState`.
        """
        return ExecutionState(
            schema_version=self.schema_version,
            databases={**self.databases, state.database_name: state},
        )

    def advanced_by(self, run_units: Iterable[Any], commit_sha: str | None) -> ExecutionState:
        """Return the state that results from a successful execution.

        A database whose run unit failed keeps its **old** watermark, so the
        next execution re-scans exactly the work that did not complete. That is
        what makes recovery from a mid-run outage automatic rather than manual.

        Args:
            run_units: The completed :class:`~src.models.execution.RunUnitResult`
                objects.
            commit_sha: The commit the changes landed in.

        Returns:
            A new state; the receiver is not modified.
        """
        state = self
        for unit in run_units:
            if unit.status is ExecutionStatus.FAILED or unit.candidate_watermark is None:
                continue
            duration = 0.0
            if unit.started_at and unit.finished_at:
                duration = (unit.finished_at - unit.started_at).total_seconds()
            state = state.with_database(
                DatabaseState(
                    database_name=unit.database,
                    last_successful_run=unit.candidate_watermark,
                    commit_sha=commit_sha,
                    execution_status=unit.status,
                    processed_objects=unit.objects_exported,
                    execution_duration_seconds=duration,
                )
            )
        return state

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible mapping.

        Returns:
            The mapping, with databases sorted by name so the serialized file
            is byte-stable and produces no spurious Git diff.
        """
        return {
            "schema_version": self.schema_version,
            "databases": {
                name: self.databases[name].to_dict() for name in sorted(self.databases)
            },
        }

    @classmethod
    def from_dict(cls, data: Any) -> ExecutionState:
        """Deserialize from a mapping, validating strictly.

        Args:
            data: The parsed JSON document.

        Returns:
            The parsed state.

        Raises:
            StateError: On any structural, type, or schema-version error.
        """
        if not isinstance(data, dict):
            raise StateError(f"state root: expected an object, got {type(data).__name__}")

        unknown = set(data) - {"schema_version", "databases"}
        if unknown:
            raise StateError(f"state root: unknown key(s) {sorted(unknown)}")

        version = data.get("schema_version", STATE_SCHEMA_VERSION)
        if version != STATE_SCHEMA_VERSION:
            raise StateError(
                f"state root: unsupported schema_version {version!r}; "
                f"this build understands {STATE_SCHEMA_VERSION}"
            )

        raw = data.get("databases", {})
        if not isinstance(raw, dict):
            raise StateError(f"state.databases: expected an object, got {type(raw).__name__}")

        databases: dict[str, DatabaseState] = {}
        for name, entry in raw.items():
            state = DatabaseState.from_dict(entry, f"state.databases[{name!r}]")
            if state.database_name != name:
                raise StateError(
                    f"state.databases[{name!r}]: database_name is "
                    f"{state.database_name!r}, which contradicts its key"
                )
            databases[name] = state

        return cls(schema_version=version, databases=databases)
