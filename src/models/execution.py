"""Execution result aggregates and exit-code semantics.

See docs/architecture.md §2, §13.2 (summary fields) and §14.5 (exit codes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path

from src.models.oracle_object import ObjectStatus, OracleObject

__all__ = [
    "ExecutionResult",
    "ExecutionStatus",
    "ExitCode",
    "ObjectResult",
    "RunUnitResult",
]


class ExitCode(IntEnum):
    """Process exit codes (docs/architecture.md §14.5)."""

    SUCCESS = 0
    FAILURE = 1
    CONFIGURATION = 2


class ExecutionStatus(StrEnum):
    """Aggregate status of a run unit or of the whole execution."""

    SUCCESS = "SUCCESS"
    NO_CHANGES = "NO_CHANGES"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ObjectResult:
    """The outcome of processing one object."""

    obj: OracleObject
    status: ObjectStatus
    path: Path | None = None
    message: str | None = None


@dataclass(slots=True)
class RunUnitResult:
    """The outcome of one database's extraction phase.

    Carries exactly the fields the per-run-unit summary log requires
    (docs/architecture.md §13.2).
    """

    database: str
    status: ExecutionStatus = ExecutionStatus.SUCCESS
    started_at: datetime | None = None
    finished_at: datetime | None = None
    objects_discovered: int = 0
    objects_exported: int = 0
    objects_skipped: int = 0
    objects_failed: int = 0
    object_results: list[ObjectResult] = field(default_factory=list)
    written_paths: list[Path] = field(default_factory=list)
    deleted_paths: list[Path] = field(default_factory=list)
    candidate_watermark: datetime | None = None
    #: Reconciliation would have deleted more than the safety threshold, so
    #: nothing was deleted for this database (decision D-6).
    reconciliation_blocked: bool = False
    #: The inventory query failed, so reconciliation did not run at all. A
    #: transient failure must never be read as "everything was dropped".
    reconciliation_skipped: bool = False


@dataclass(slots=True)
class ExecutionResult:
    """The outcome of one execution, aggregating every run unit."""

    execution_id: str
    status: ExecutionStatus = ExecutionStatus.SUCCESS
    started_at: datetime | None = None
    finished_at: datetime | None = None
    run_units: list[RunUnitResult] = field(default_factory=list)
    commit_sha: str | None = None
    pushed: bool = False

    @property
    def exit_code(self) -> ExitCode:
        """Map the aggregate status to a process exit code."""
        if self.status in (ExecutionStatus.SUCCESS, ExecutionStatus.NO_CHANGES):
            return ExitCode.SUCCESS
        return ExitCode.FAILURE
