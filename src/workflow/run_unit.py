"""One schema's extraction phase (docs/architecture.md §1.2).

    discover -> extract -> normalize -> write -> reconcile

The unit of work is the **schema**, not the database (decision D-15). The
caller opens one session per database and drives its schemas through this class
one at a time, committing and pushing each before the next begins.

Sequential *within* a schema: that is a requirement, and it also keeps
``DBMS_METADATA`` session transform state predictable (§1.3).

Object-level failure is still contained here — one bad object never stops the
schema. Anything larger propagates: a ``DatabaseError`` raised from
:meth:`RunUnit.run` stops the whole execution (decision D-17), which is safe
because every schema completed before this one has already been pushed and
recorded.
"""

from __future__ import annotations

from time import monotonic

from src.config.models import DatabaseConfig, SchemaFilter, Settings
from src.exceptions import (
    DatabaseError,
    DiscoveryError,
    NormalizationError,
    ObjectError,
    RepositoryError,
)
from src.logging.logger import get_logger
from src.models.database import SchemaRuntime
from src.models.execution import ExecutionStatus, ObjectResult, RunUnitResult
from src.models.oracle_object import ObjectStatus, OracleObject
from src.normalizer.ddl_normalizer import DdlNormalizer
from src.oracle.connection import Session
from src.oracle.metadata import ExtractionDispatcher, ObjectVanished
from src.oracle.object_discovery import ObjectDiscovery
from src.repository.comparer import Reconciler
from src.repository.file_mapper import FileMapper
from src.repository.writer import FileWriter, Writer
from src.utils.timers import Timer, utc_now

__all__ = ["RunUnit"]

_log = get_logger(__name__)


class RunUnit:
    """Processes exactly one schema of one database."""

    def __init__(
        self,
        database: DatabaseConfig,
        runtime: SchemaRuntime,
        settings: Settings,
        session: Session,
        normalizer: DdlNormalizer | None = None,
        writer: Writer | None = None,
    ) -> None:
        """Create a run unit.

        Collaborators are injected so tests substitute fakes without patching
        modules (docs/architecture.md §2).

        Args:
            database: The database this schema belongs to.
            runtime: The resolved runtime view, carrying this schema's watermark.
            settings: Global settings.
            session: An initialized read-only session, owned by the caller and
                shared across this database's schemas.
            normalizer: Applies the normalization pipeline.
            writer: Writes files. A ``NullWriter`` makes the run a dry run.
        """
        self.database = database
        self.runtime = runtime
        self.schema = runtime.schema
        self.settings = settings
        self.session = session
        self.normalizer = normalizer or DdlNormalizer(settings.normalization)
        self.writer = writer or FileWriter()
        self.mapper = FileMapper(runtime.git_folder, database.name)
        self.reconciler = Reconciler(settings.reconciliation, self.mapper)

    def run(self) -> RunUnitResult:
        """Extract, normalize, and write this schema, then reconcile deletions.

        Returns:
            The schema's outcome, ready for the Git phase.

        Raises:
            src.exceptions.DatabaseError: On a discovery failure. Unlike the
                previous per-database model this is **not** caught here: it
                stops the execution (decision D-17), and the schemas already
                pushed keep their advanced watermarks.
        """
        result = RunUnitResult(
            database=self.database.name, schema=self.schema, started_at=utc_now()
        )
        extra = {"database": self.database.name, "schema": self.schema}

        if self.runtime.is_full_export:
            _log.info("%s: no watermark; performing a full export", self.schema, extra=extra)
        else:
            _log.info(
                "%s: discovering objects changed since %s",
                self.schema, self.runtime.watermark, extra=extra,
            )

        with Timer() as timer:
            self._process(result)

        changed = result.written_paths or result.deleted_paths
        result.status = ExecutionStatus.SUCCESS if changed else ExecutionStatus.NO_CHANGES
        _log.info(
            "%s: discovered=%d exported=%d skipped=%d failed=%d deleted=%d in %.2fs",
            self.schema,
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

    def _process(self, result: RunUnitResult) -> None:
        """Read the database clock, discover, extract each object, reconcile.

        Args:
            result: The run unit result, updated in place.

        Raises:
            src.exceptions.DatabaseError: On a discovery failure or a wall-clock
                timeout.
        """
        # The candidate watermark is read from the *database* clock before
        # discovery, so nothing that changes during the run can be missed. It
        # is persisted only after this schema's push (docs/architecture.md §4.2).
        result.candidate_watermark = self.session.database_time()

        discovery = ObjectDiscovery(
            self.session, self.database, self.settings, schema=self.schema
        )
        objects = discovery.discover_changed(self.runtime)
        result.objects_discovered = len(objects)

        # A wall-clock budget, checked between objects (§15). Threads cannot be
        # killed, so the deadline is cooperative: it stops the run unit from
        # starting further work rather than interrupting work in flight.
        deadline = monotonic() + self.settings.retry.run_unit_timeout_seconds
        dispatcher = ExtractionDispatcher(self.session, self.settings)
        for index, obj in enumerate(objects):
            if monotonic() > deadline:
                raise DatabaseError(
                    f"{self.database.name}.{self.schema}: wall-clock timeout of "
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
        extra = {"database": self.database.name, "schema": self.schema}
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
            # here: it ends the execution rather than one object.
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
        """Stage deletions for this schema's files that have no matching object.

        Scoped to the schema being processed (§9.3): both the inventory and the
        path enumeration cover this schema alone, so the D-6 threshold is a
        percentage of *this schema's* files. A database-wide denominator would
        let a small schema be wiped entirely while staying under 10%.

        A failed inventory query suppresses reconciliation rather than ending
        the run: the objects already extracted are legitimate, and a transient
        failure must never be read as "everything was dropped".

        Args:
            discovery: The schema-scoped discovery service.
            result: The run unit result, updated in place.
        """
        extra = {"database": self.database.name, "schema": self.schema}
        try:
            inventory = discovery.full_inventory()
        except DiscoveryError as exc:
            _log.warning(
                "%s: reconciliation skipped, inventory query failed: %s",
                self.schema, exc, extra=extra,
            )
            result.reconciliation_skipped = True
            return

        existing = self.reconciler.enumerate_paths(SchemaFilter(include=(self.schema,)))
        try:
            obsolete = self.reconciler.obsolete_paths(inventory, existing)
        except Exception as exc:
            _log.error(
                "%s: reconciliation skipped, cannot map inventory: %s",
                self.schema, exc, extra=extra,
            )
            result.reconciliation_skipped = True
            return

        if self.reconciler.exceeds_threshold(len(obsolete), len(existing)):
            _log.error(
                "%s: reconciliation BLOCKED: %d of %d file(s) would be deleted, over the "
                "%.1f%% threshold. Nothing is deleted. Re-run with --allow-mass-delete if "
                "this is intended.",
                self.schema, len(obsolete), len(existing),
                self.settings.reconciliation.mass_delete_threshold_percent,
                extra=extra,
            )
            result.reconciliation_blocked = True
            return

        if obsolete:
            _log.info(
                "%s: %d file(s) no longer have a matching object",
                self.schema, len(obsolete), extra=extra,
            )
        result.deleted_paths.extend(obsolete)
