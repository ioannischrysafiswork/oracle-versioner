"""One database's extraction phase (docs/architecture.md §1.2).

    connect -> discover -> extract -> normalize -> write -> reconcile

Sequential *within* a database: that is a requirement, and it also keeps
``DBMS_METADATA`` session transform state predictable (§1.3).

Failure is contained here. An object-level failure never stops the database; a
database-level failure never stops the other databases (§0.6). That is why
:meth:`RunUnit.run` reports a failed database as a *status* rather than raising.

PHASE 5 — the extraction phase is complete end to end. The Git phase (staging,
commit, push, state) is Phase 6, so the paths collected here are reported but
not yet committed.
"""

from __future__ import annotations

from time import monotonic

from src.config.models import DatabaseConfig, Settings
from src.exceptions import (
    ConnectionError as OracleConnectionError,
)
from src.exceptions import (
    DatabaseError,
    DiscoveryError,
    NormalizationError,
    ObjectError,
    RepositoryError,
)
from src.logging.logger import get_logger
from src.models.database import DatabaseRuntime
from src.models.execution import ExecutionStatus, ObjectResult, RunUnitResult
from src.models.oracle_object import ObjectStatus, OracleObject
from src.normalizer.ddl_normalizer import DdlNormalizer
from src.oracle.connection import ConnectionFactory, Session, preflight
from src.oracle.metadata import ExtractionDispatcher, ObjectVanished
from src.oracle.object_discovery import ObjectDiscovery
from src.repository.comparer import Reconciler
from src.repository.file_mapper import FileMapper
from src.repository.writer import FileWriter, Writer
from src.utils.timers import Timer, utc_now

__all__ = ["RunUnit"]

_log = get_logger(__name__)


class RunUnit:
    """Processes exactly one database, independently of all others."""

    def __init__(
        self,
        database: DatabaseConfig,
        runtime: DatabaseRuntime,
        settings: Settings,
        connection_factory: ConnectionFactory,
        normalizer: DdlNormalizer | None = None,
        writer: Writer | None = None,
    ) -> None:
        """Create a run unit.

        Collaborators are injected so tests substitute fakes without patching
        modules (docs/architecture.md §2).

        Args:
            database: The database this unit is responsible for.
            runtime: The resolved runtime view, carrying the watermark.
            settings: Global settings.
            connection_factory: Supplies an initialized read-only session.
            normalizer: Applies the normalization pipeline.
            writer: Writes files. A ``NullWriter`` makes the run a dry run.
        """
        self.database = database
        self.runtime = runtime
        self.settings = settings
        self.connection_factory = connection_factory
        self.normalizer = normalizer or DdlNormalizer(settings.normalization)
        self.writer = writer or FileWriter()
        self.mapper = FileMapper(runtime.git_folder, database.name)
        self.reconciler = Reconciler(settings.reconciliation, self.mapper)

    def run(self) -> RunUnitResult:
        """Execute the extraction phase for this database.

        Returns:
            The run unit's outcome. A contained database-level failure is
            reported as ``FAILED``, never raised — other databases must continue.
        """
        result = RunUnitResult(database=self.database.name, started_at=utc_now())
        extra = {"database": self.database.name}

        if self.runtime.is_full_export:
            _log.info("no watermark; performing a full export", extra=extra)
        else:
            _log.info("discovering objects changed since %s", self.runtime.watermark, extra=extra)

        try:
            with Timer() as timer, self.connection_factory.session(self.database) as session:
                self._process(session, result)
        except DatabaseError as exc:
            _log.error("database failed, other databases continue: %s", exc, extra=extra)
            result.status = ExecutionStatus.FAILED
        else:
            changed = result.written_paths or result.deleted_paths
            result.status = ExecutionStatus.SUCCESS if changed else ExecutionStatus.NO_CHANGES
            _log.info(
                "discovered=%d exported=%d skipped=%d failed=%d deleted=%d in %.2fs",
                result.objects_discovered,
                result.objects_exported,
                result.objects_skipped,
                result.objects_failed,
                len(result.deleted_paths),
                timer.elapsed,
                extra=extra,
            )

        result.finished_at = utc_now()
        return result

    def _process(self, session: Session, result: RunUnitResult) -> None:
        """Read the database clock, discover, extract each object, reconcile.

        Args:
            session: An initialized read-only session.
            result: The run unit result, updated in place.

        Raises:
            src.exceptions.DatabaseError: On a connection or discovery failure,
                which ends this database but no other.
        """
        # Fail fast and by name if a grant is missing, rather than part-way
        # through a run with half the objects extracted (§21.2).
        missing = preflight(session)
        if missing:
            raise OracleConnectionError(
                f"{self.database.name}: cannot read {missing}; check the account's grants "
                f"(CREATE SESSION, SELECT_CATALOG_ROLE, EXECUTE ON DBMS_METADATA)"
            )

        # The candidate watermark is read from the *database* clock before
        # discovery, so nothing that changes during the run can be missed. It
        # is persisted only after a successful push (docs/architecture.md §4.2).
        result.candidate_watermark = session.database_time()

        discovery = ObjectDiscovery(session, self.database, self.settings)
        objects = discovery.discover_changed(self.runtime)
        result.objects_discovered = len(objects)

        # A wall-clock budget, checked between objects (§15). Threads cannot be
        # killed, so the deadline is cooperative: it stops the run unit from
        # starting further work rather than interrupting work in flight. That
        # is enough to keep one slow database from consuming the whole job
        # timeout, which is what the guard exists for.
        deadline = monotonic() + self.settings.retry.run_unit_timeout_seconds
        dispatcher = ExtractionDispatcher(session, self.settings)
        for index, obj in enumerate(objects):
            if monotonic() > deadline:
                raise DatabaseError(
                    f"{self.database.name}: wall-clock timeout of "
                    f"{self.settings.retry.run_unit_timeout_seconds}s exceeded after "
                    f"{index} of {len(objects)} object(s)"
                )
            self._process_one(dispatcher, obj, result)

        if self.settings.reconciliation.enabled:
            self._reconcile(discovery, result)

    def _process_one(
        self, dispatcher: ExtractionDispatcher, obj: OracleObject, result: RunUnitResult
    ) -> None:
        """Extract, normalize, and write one object, containing any failure.

        Args:
            dispatcher: The extraction dispatcher for this session.
            obj: The object to process.
            result: The run unit result, updated in place.
        """
        extra = {"database": self.database.name}
        try:
            source = dispatcher.extract(obj)
            normalized = self.normalizer.normalize(source.text, obj.object_type)
            path = self.mapper.to_path(obj)
            self.writer.write(path, normalized)
        except ObjectVanished:
            # Dropped between discovery and extraction. Not a failure:
            # reconciliation removes the file (docs/architecture.md §22.4).
            _log.warning(
                "%s.%s (%s) no longer exists; skipped",
                obj.owner, obj.object_name, obj.object_type, extra=extra,
            )
            result.objects_skipped += 1
            result.object_results.append(ObjectResult(obj=obj, status=ObjectStatus.SKIPPED))
            return
        except (ObjectError, RepositoryError) as exc:
            # Object-scoped only. A DatabaseError is deliberately not caught
            # here: it ends the run unit rather than one object.
            status = self._status_for(exc)
            _log.warning(
                "%s.%s (%s) failed: %s",
                obj.owner, obj.object_name, obj.object_type, exc, extra=extra,
            )
            result.objects_failed += 1
            result.object_results.append(
                ObjectResult(obj=obj, status=status, message=str(exc))
            )
            return

        result.objects_exported += 1
        result.written_paths.append(path)
        result.object_results.append(ObjectResult(obj=obj, status=ObjectStatus.SUCCESS, path=path))

    @staticmethod
    def _status_for(exc: BaseException) -> ObjectStatus:
        """Map an object-scoped exception to its per-object status."""
        if isinstance(exc, NormalizationError):
            return ObjectStatus.FAILED_NORMALIZATION
        if isinstance(exc, ObjectError):
            return ObjectStatus.FAILED_EXTRACTION
        # Mapping and write failures share FAILED_COMPARISON per §14.3.
        return ObjectStatus.FAILED_COMPARISON

    def _reconcile(self, discovery: ObjectDiscovery, result: RunUnitResult) -> None:
        """Stage deletions for repository files with no corresponding object.

        A failed inventory query suppresses reconciliation entirely rather than
        ending the run unit: the objects already extracted are legitimate, and
        a transient failure must never be read as "everything was dropped".

        Args:
            discovery: The discovery service for this session.
            result: The run unit result, updated in place.
        """
        extra = {"database": self.database.name}
        try:
            inventory = discovery.full_inventory()
        except DiscoveryError as exc:
            _log.warning("reconciliation skipped, inventory query failed: %s", exc, extra=extra)
            result.reconciliation_skipped = True
            return

        existing = self.reconciler.enumerate_paths(self.database.schemas)
        try:
            obsolete = self.reconciler.obsolete_paths(inventory, existing)
        except Exception as exc:
            _log.error("reconciliation skipped, cannot map inventory: %s", exc, extra=extra)
            result.reconciliation_skipped = True
            return

        if self.reconciler.exceeds_threshold(len(obsolete), len(existing)):
            _log.error(
                "reconciliation BLOCKED: %d of %d file(s) would be deleted, over the %.1f%% "
                "threshold. Nothing is deleted. Re-run with --allow-mass-delete if this is "
                "intended.",
                len(obsolete), len(existing),
                self.settings.reconciliation.mass_delete_threshold_percent,
                extra=extra,
            )
            result.reconciliation_blocked = True
            return

        if obsolete:
            _log.info("%d file(s) no longer have a matching object", len(obsolete), extra=extra)
        result.deleted_paths.extend(obsolete)
