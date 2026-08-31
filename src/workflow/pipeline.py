"""The execution pipeline (docs/architecture.md §1.2).

One execution = one process = one synchronization = exit.

    load config                      (fatal on invalid)
    load execution_state.json        (fatal on parse error, migrate v1 -> v2)
    for each database, sequentially:
        connect -> resolve the schema list
        for each schema, sequentially:
            discover -> extract -> normalize -> write -> reconcile
            stage -> commit -> push -> write this schema's state -> amend -> push
            any failure -> STOP the execution (decision D-17)
    emit summary -> exit code

The unit of work is the **schema** (decision D-15). Each is carried all the way
to a pushed commit before the next begins, so an execution that fails partway
leaves every completed schema recorded and the next run resumes at the one that
failed.

Execution is sequential throughout (decision D-16): the Git index is one shared
mutable resource, so per-schema commits and concurrent extraction cannot
coexist. See docs/architecture.md §1.3 for the full reasoning.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from src.config.models import AppConfig, DatabaseConfig
from src.config.secrets import SecretProvider
from src.exceptions import DatabaseError, GitError
from src.git.git_client import GitClient
from src.logging.logger import get_logger
from src.models.database import SchemaRuntime
from src.models.execution import ExecutionResult, ExecutionStatus
from src.oracle.connection import ConnectionFactory, Session, preflight
from src.oracle.object_discovery import ObjectDiscovery
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
    """Drives every database's schemas, one complete cycle at a time."""

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
            The aggregated execution result, whose ``exit_code`` the entry
            point uses as the process exit status.
        """
        started = utc_now()
        result = ExecutionResult(execution_id=self.execution_id, started_at=started)
        databases = self.config.enabled_databases
        _log.info(
            "execution started: %d database(s), dry_run=%s, no_git=%s",
            len(databases), self.config.dry_run, self.config.no_git,
        )

        phase = GitPhase(
            self.config, self.state, self.state_store, {},
            self.git_client, self.secret_provider,
        )
        try:
            phase.prepare()
            # Once per execution: a stranded commit is a property of the
            # repository, and the check costs a network round trip.
            stranded = phase.push_stranded_commit()
            if stranded:
                result.commit_sha = stranded
                result.pushed = True

            for database in databases:
                self._process_database(database, phase, result)
        except (DatabaseError, GitError) as exc:
            # Decision D-17: stop here. Every schema already pushed keeps its
            # advanced watermark, so nothing completed is lost and the next
            # run resumes at the schema that failed.
            _log.error("execution stopped: %s", exc)
            result.status = ExecutionStatus.FAILED
            result.stopped_by = str(exc)
        else:
            result.status = self._aggregate(result)
        finally:
            self.connection_factory.close()
            self.state = phase.state

        result.finished_at = utc_now()
        self._log_summary(result)
        return result

    def _process_database(
        self, database: DatabaseConfig, phase: GitPhase, result: ExecutionResult
    ) -> None:
        """Connect once, then drive this database's schemas one at a time.

        Args:
            database: The database to process.
            phase: The Git phase, shared across schemas so state accumulates.
            result: The execution result, updated in place.

        Raises:
            src.exceptions.DatabaseError: On a connection, preflight, or
                discovery failure — which stops the execution (D-17).
            src.exceptions.GitError: On a commit or push failure — likewise.
        """
        extra = {"database": database.name}
        # Dry-run suppression is a substituted collaborator, never a branch in
        # the business logic, so a dry run exercises the same code path and no
        # stray write can survive (docs/architecture.md §16).
        writer: Writer = NullWriter() if self.config.dry_run else FileWriter()

        with self.connection_factory.session(database) as session:
            self._preflight(session, database)
            schemas = ObjectDiscovery(session, database, self.config.settings).list_schemas()
            self._warn_about_absent_schemas(database, schemas)

            for schema in schemas:
                runtime = self._runtime_for(database, schema, phase.state)
                mapper = FileMapper(runtime.git_folder, database.name)
                phase.mappers = {database.name: mapper}

                unit = RunUnit(
                    database, runtime, self.config.settings, session, writer=writer
                ).run()
                result.run_units.append(unit)

                if phase.run(unit):
                    result.commit_sha = unit.commit_sha or result.commit_sha
                    result.pushed = result.pushed or unit.pushed
                else:
                    unit.status = ExecutionStatus.NO_CHANGES

                _log.info(
                    "%s: complete (status=%s)", schema, unit.status, extra=extra
                )

    @staticmethod
    def _preflight(session: Session, database: DatabaseConfig) -> None:
        """Fail fast and by name if a grant is missing.

        Args:
            session: The initialized session.
            database: The database being checked.

        Raises:
            src.exceptions.DatabaseError: If a required view is unreadable.
        """
        missing = preflight(session)
        if missing:
            raise DatabaseError(
                f"{database.name}: cannot read {missing}; check the account's grants "
                f"(CREATE SESSION, SELECT_CATALOG_ROLE, EXECUTE ON DBMS_METADATA)"
            )

    @staticmethod
    def _warn_about_absent_schemas(database: DatabaseConfig, resolved: list[str]) -> None:
        """Name configured schemas the database did not report.

        Such a schema is never processed, so its files are left alone rather
        than reconciled away — safe, but indistinguishable from working
        correctly if the name was simply mistyped.

        Args:
            database: The database whose filter is being checked.
            resolved: The schemas the database reported.
        """
        configured = {name.upper() for name in database.schemas.include}
        if not configured:
            return
        absent = sorted(configured - {name.upper() for name in resolved})
        if absent:
            _log.warning(
                "configured schema(s) %s hold no objects in scope and are not processed; "
                "their existing files are left untouched. Check for a typo or a missing "
                "grant.",
                ", ".join(absent),
                extra={"database": database.name},
            )

    def _runtime_for(
        self, database: DatabaseConfig, schema: str, state: ExecutionState
    ) -> SchemaRuntime:
        """Resolve the runtime view of one schema, including its watermark.

        The configured overlap is subtracted here (docs/architecture.md §4.2):
        ``LAST_DDL_TIME`` has one-second granularity, so DDL committed just
        after the watermark was captured could otherwise be missed.

        Args:
            database: The configured database.
            schema: The schema being processed.
            state: The current state — read fresh for each schema, because an
                earlier schema in this run may already have advanced it.

        Returns:
            The runtime descriptor, its ``watermark`` already overlap-adjusted.
            ``None`` means a full export, which is what a new schema gets.
        """
        watermark = state.watermark_for(database.name, schema)
        if watermark is not None:
            watermark -= timedelta(minutes=self.config.settings.watermark_overlap_minutes)
        return SchemaRuntime(
            name=database.name,
            schema=schema,
            git_folder=Path(self.config.settings.repository_root) / database.git_folder,
            watermark=watermark,
        )

    def _log_summary(self, result: ExecutionResult) -> None:
        """Emit the per-schema and execution summaries (docs/architecture.md §13.2)."""
        for unit in result.run_units:
            _log.info(
                "summary: %s.%s status=%s discovered=%d exported=%d skipped=%d failed=%d "
                "deleted=%d pushed=%s",
                unit.database, unit.schema, unit.status,
                unit.objects_discovered, unit.objects_exported,
                unit.objects_skipped, unit.objects_failed, len(unit.deleted_paths),
                unit.pushed,
                extra={"database": unit.database},
            )

        elapsed = 0.0
        if result.started_at and result.finished_at:
            elapsed = (result.finished_at - result.started_at).total_seconds()

        if result.stopped_by:
            completed = [f"{u.database}.{u.schema}" for u in result.run_units if u.pushed]
            _log.error(
                "execution STOPPED after %d schema(s): %s. Pushed and recorded before the "
                "failure: %s",
                len(result.run_units),
                result.stopped_by,
                ", ".join(completed) or "none",
            )

        _log.info(
            "execution finished: status=%s duration=%s schemas=%d commit=%s pushed=%s",
            result.status, format_duration(elapsed), len(result.run_units),
            result.commit_sha or "-", result.pushed,
        )

    @staticmethod
    def _aggregate(result: ExecutionResult) -> ExecutionStatus:
        """Reduce the per-schema statuses to one execution status.

        Only reached when nothing stopped the execution, so there is no
        ``FAILED`` unit to consider — D-17 turns that into an early exit.

        Args:
            result: The execution whose run units should be aggregated.

        Returns:
            ``NO_CHANGES`` when nothing was pushed, else ``SUCCESS``.
        """
        if not result.run_units:
            return ExecutionStatus.NO_CHANGES
        if all(unit.status is ExecutionStatus.NO_CHANGES for unit in result.run_units):
            # A stranded commit pushed at the start is still a real change,
            # even though no schema produced one.
            return ExecutionStatus.SUCCESS if result.pushed else ExecutionStatus.NO_CHANGES
        return ExecutionStatus.SUCCESS
