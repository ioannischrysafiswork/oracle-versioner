"""The execution pipeline (docs/architecture.md §1.2).

One execution = one process = one synchronization = exit.

    load config                      (fatal on invalid)
    load execution_state.json        (fatal on parse error)
    EXTRACTION PHASE                 concurrent across databases,
                                     sequential within a database
    GIT PHASE                        serialized, once per execution
        nothing staged -> exit 0, no commit, no state update
        commit -> push -> update state -> amend -> push
    emit summary -> exit code

This module owns the extraction phase and the summary; the Git phase is
delegated to :class:`src.workflow.git_phase.GitPhase`, mirroring the two stages
§1.2 defines.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from src.config.models import AppConfig, DatabaseConfig
from src.config.secrets import SecretProvider
from src.git.git_client import GitClient
from src.logging.logger import get_logger
from src.models.database import DatabaseRuntime
from src.models.execution import ExecutionResult, ExecutionStatus, RunUnitResult
from src.oracle.connection import ConnectionFactory
from src.repository.file_mapper import FileMapper
from src.repository.writer import FileWriter, NullWriter, Writer
from src.state.execution_state import ExecutionStateStore
from src.state.models import ExecutionState
from src.utils.timers import format_duration, utc_now
from src.workflow.git_phase import GitPhase
from src.workflow.run_unit import RunUnit

__all__ = ["Pipeline"]

_log = get_logger(__name__)


class Pipeline:
    """Coordinates the extraction phase and the Git phase for one execution."""

    def __init__(
        self,
        config: AppConfig,
        state: ExecutionState,
        state_store: ExecutionStateStore,
        execution_id: str | None = None,
        connection_factory: ConnectionFactory | None = None,
        git_client: GitClient | None = None,
        secret_provider: SecretProvider | None = None,
    ) -> None:
        """Create a pipeline.

        Args:
            config: The validated configuration for this execution.
            state: The state loaded before the run started.
            state_store: Where state is persisted — only after a successful push.
            execution_id: Correlation id; generated when omitted.
            connection_factory: Supplies Oracle sessions. Tests inject a fake;
                omitting it builds the real, pooled factory.
            git_client: Drives ``git``. Defaults to the current directory,
                which is the CI checkout.
            secret_provider: Resolves the push token at call time.
        """
        self.config = config
        self.state = state
        self.state_store = state_store
        self.execution_id = execution_id or uuid.uuid4().hex[:12]
        self.connection_factory = connection_factory or ConnectionFactory(config.settings)
        self.git_client = git_client or GitClient(Path.cwd())
        self.secret_provider = secret_provider

    def run(self) -> ExecutionResult:
        """Run one synchronization.

        Returns:
            The aggregated execution result, whose ``exit_code`` the entry point
            uses as the process exit status.
        """
        started = utc_now()
        result = ExecutionResult(execution_id=self.execution_id, started_at=started)
        databases = self.config.enabled_databases
        _log.info(
            "execution started: %d database(s), dry_run=%s, no_git=%s",
            len(databases),
            self.config.dry_run,
            self.config.no_git,
        )

        result.run_units = self._extraction_phase(databases)

        phase = GitPhase(
            self.config, self.state, self.state_store, self._mappers(),
            self.git_client, self.secret_provider,
        )
        effective = phase.run(result)
        self.state = phase.state

        result.status = self._aggregate(result)
        if not effective and result.status is ExecutionStatus.SUCCESS:
            # Files were written but produced no diff: Git is the precise gate,
            # so the execution reports NO_CHANGES rather than a phantom success.
            result.status = ExecutionStatus.NO_CHANGES
        result.finished_at = utc_now()
        self._log_summary(result)
        return result

    def _extraction_phase(
        self, databases: tuple[DatabaseConfig, ...]
    ) -> list[RunUnitResult]:
        """Run every database's run unit, concurrently across databases.

        Args:
            databases: The enabled databases.

        Returns:
            One result per database, in configuration order regardless of the
            order in which the threads finished — so the summary and the commit
            body stay deterministic.
        """
        if not databases:
            return []

        # Dry-run suppression is a substituted collaborator, never a branch in
        # the business logic, so a dry run exercises the same code path and no
        # stray write can survive (docs/architecture.md §16).
        writer: Writer = NullWriter() if self.config.dry_run else FileWriter()

        units = [
            RunUnit(
                database,
                self._runtime_for(database),
                self.config.settings,
                self.connection_factory,
                writer=writer,
            )
            for database in databases
        ]
        try:
            if len(units) == 1:
                return [units[0].run()]

            workers = min(self.config.settings.max_workers, len(units))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="run-unit") as pool:
                return list(pool.map(lambda unit: unit.run(), units))
        finally:
            self.connection_factory.close()

    def _runtime_for(self, database: DatabaseConfig) -> DatabaseRuntime:
        """Resolve the runtime view of ``database``, including its watermark.

        The configured overlap is subtracted here (docs/architecture.md §4.2):
        ``LAST_DDL_TIME`` has one-second granularity, so DDL committed just
        after the watermark was captured could otherwise be missed. Re-reading
        an unchanged object costs only extraction time, because a re-written
        identical file produces no Git diff.

        Args:
            database: The configured database.

        Returns:
            The runtime descriptor. Its ``watermark`` is already
            overlap-adjusted and ready to bind into the discovery query; a
            ``None`` watermark means a full export.
        """
        watermark = self.state.watermark_for(database.name)
        if watermark is not None:
            watermark -= timedelta(minutes=self.config.settings.watermark_overlap_minutes)
        return DatabaseRuntime(
            name=database.name,
            git_folder=Path(self.config.settings.repository_root) / database.git_folder,
            watermark=watermark,
        )

    def _mappers(self) -> dict[str, FileMapper]:
        """Path mappers per database, for describing the staged set."""
        return {
            database.name: FileMapper(self._runtime_for(database).git_folder, database.name)
            for database in self.config.enabled_databases
        }

    def _log_summary(self, result: ExecutionResult) -> None:
        """Emit the per-run-unit and execution summaries (docs/architecture.md §13.2)."""
        for unit in result.run_units:
            _log.info(
                "summary: status=%s discovered=%d exported=%d skipped=%d failed=%d deleted=%d",
                unit.status, unit.objects_discovered, unit.objects_exported,
                unit.objects_skipped, unit.objects_failed, len(unit.deleted_paths),
                extra={"database": unit.database},
            )

        elapsed = 0.0
        if result.started_at and result.finished_at:
            elapsed = (result.finished_at - result.started_at).total_seconds()
        _log.info(
            "execution finished: status=%s duration=%s commit=%s pushed=%s",
            result.status, format_duration(elapsed), result.commit_sha or "-", result.pushed,
        )

    @staticmethod
    def _aggregate(result: ExecutionResult) -> ExecutionStatus:
        """Reduce the run-unit statuses to one execution status.

        Args:
            result: The execution whose run units should be aggregated.

        Returns:
            ``FAILED`` if every run unit failed, ``PARTIAL_FAILURE`` if some
            did, ``NO_CHANGES`` if none produced changes, else ``SUCCESS``.
        """
        statuses = [unit.status for unit in result.run_units]
        if not statuses:
            return ExecutionStatus.NO_CHANGES
        failed = [s for s in statuses if s is ExecutionStatus.FAILED]
        if len(failed) == len(statuses):
            return ExecutionStatus.FAILED
        if failed:
            return ExecutionStatus.PARTIAL_FAILURE
        if all(s is ExecutionStatus.NO_CHANGES for s in statuses):
            return ExecutionStatus.NO_CHANGES
        return ExecutionStatus.SUCCESS
