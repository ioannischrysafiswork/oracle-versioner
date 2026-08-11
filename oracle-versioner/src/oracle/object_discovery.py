"""Change discovery and full inventory against ``DBA_OBJECTS`` (docs/architecture.md §4).

Two queries share one predicate set; the only difference is that the inventory
query omits the ``LAST_DDL_TIME`` predicate. The statement text is assembled in
:mod:`src.oracle.sql` so that every SQL string in the project sits in one
auditable file; this module applies the filters, runs the query, and maps rows.

Ordering is ``OWNER, OBJECT_TYPE, OBJECT_NAME`` — required, and it also makes
the object list in the commit body deterministic.
"""

from __future__ import annotations

from datetime import datetime

from src.config.models import DatabaseConfig, Settings
from src.exceptions import DiscoveryError
from src.logging.logger import get_logger
from src.models.database import DatabaseRuntime
from src.models.oracle_object import ObjectType, OracleObject, OracleStatus
from src.oracle import sql
from src.oracle.connection import Session, is_transient
from src.utils.retry import RetryPolicy, retry_call

__all__ = ["ObjectDiscovery"]

_log = get_logger(__name__)

#: ``ORACLE_MAINTAINED`` was added in 12c; below that the static deny-list in
#: :data:`src.oracle.sql.INTERNAL_SCHEMAS` is the only defence.
_ORACLE_MAINTAINED_MIN_VERSION = 12


class ObjectDiscovery:
    """Builds and runs the ``DBA_OBJECTS`` queries for one database."""

    def __init__(self, session: Session, database: DatabaseConfig, settings: Settings) -> None:
        """Create a discovery service.

        Args:
            session: An initialized read-only session.
            database: The database being processed, supplying the filters.
            settings: Global settings, supplying the retry budget.
        """
        self.session = session
        self.database = database
        self.settings = settings

    def discover_changed(self, runtime: DatabaseRuntime) -> list[OracleObject]:
        """Return objects whose ``LAST_DDL_TIME`` is past the watermark.

        On a full export (no watermark) the predicate is omitted entirely and
        every in-scope object is returned.

        Args:
            runtime: The database being processed, carrying its watermark.

        Returns:
            Objects ordered by ``OWNER, OBJECT_TYPE, OBJECT_NAME``, deduplicated
            by ``(owner, type, name)``.

        Raises:
            DiscoveryError: If the query fails after its retry budget.
        """
        incremental = not runtime.is_full_export
        binds_extra: dict = {}
        if incremental and runtime.watermark is not None:
            # `LAST_DDL_TIME` is a DATE holding *server-local* wall time, with
            # no zone. Binding a timezone-aware value would make Oracle convert
            # it using the SESSION timezone — which is the client's, not the
            # server's — and a mismatch there silently skips every change in
            # the intervening hours. Converting to server-local naive means no
            # conversion happens at all, and like is compared with like.
            binds_extra["watermark"] = self._to_server_local(runtime.watermark)
        return self._run(incremental=incremental, extra_binds=binds_extra, what="discovery")

    def _to_server_local(self, moment: datetime) -> datetime:
        """Express ``moment`` as the server's naive local wall time.

        Args:
            moment: A timezone-aware instant, as stored in the state file.

        Returns:
            The same instant in the server's zone, with ``tzinfo`` removed.
        """
        if moment.tzinfo is None:
            return moment
        server_now = self.session.database_time()
        return moment.astimezone(server_now.tzinfo).replace(tzinfo=None)

    def full_inventory(self) -> list[OracleObject]:
        """Return the complete in-scope object list, metadata only.

        This feeds deleted-object reconciliation (docs/architecture.md §9); no
        source is extracted.

        Returns:
            Every in-scope object, in the same order as :meth:`discover_changed`.

        Raises:
            DiscoveryError: If the query fails after its retry budget.
        """
        return self._run(incremental=False, extra_binds={}, what="inventory")

    def _run(
        self, *, incremental: bool, extra_binds: dict, what: str
    ) -> list[OracleObject]:
        """Assemble, execute, and map one discovery query.

        Args:
            incremental: Whether to include the watermark predicate.
            extra_binds: Additional binds, i.e. the watermark.
            what: ``discovery`` or ``inventory``, for log messages.

        Returns:
            The mapped, deduplicated object list.

        Raises:
            DiscoveryError: If the query fails after its retry budget.
        """
        statement, binds = sql.build_discovery_query(
            object_types=tuple(str(t) for t in self.database.object_types.types),
            include_schemas=self.database.schemas.include,
            exclude_schemas=self.database.schemas.exclude,
            incremental=incremental,
            oracle_maintained_column=(
                self.session.server_major_version >= _ORACLE_MAINTAINED_MIN_VERSION
            ),
        )
        binds.update(extra_binds)

        retry = self.settings.retry
        policy = RetryPolicy(
            attempts=retry.query_attempts,
            base_delay=retry.connect_backoff_seconds,
            jitter_ratio=retry.jitter_ratio,
        )
        extra = {"database": self.database.name}

        try:
            rows = retry_call(
                lambda: self.session.fetch_all(statement, binds),
                policy=policy,
                is_retryable=is_transient,
                description=f"{what} query",
                on_retry=lambda attempt, delay, exc: _log.warning(
                    "%s query attempt %d failed (%s); retrying in %.1fs",
                    what, attempt, exc, delay, extra=extra,
                ),
            )
        except Exception as exc:
            raise DiscoveryError(f"{self.database.name}: {what} query failed: {exc}") from exc

        objects = self._map(rows)
        _log.info("%s returned %d object(s)", what, len(objects), extra=extra)
        return objects

    def _map(self, rows: list[tuple]) -> list[OracleObject]:
        """Map result rows to objects, dropping duplicates and unknown types.

        Args:
            rows: ``(owner, object_type, object_name, status, created, last_ddl_time)``.

        Returns:
            The mapped objects, deduplicated by ``(owner, type, name)`` so no
            object is ever extracted twice in one execution (§5.6).
        """
        seen: set[tuple[str, str, str]] = set()
        objects: list[OracleObject] = []

        for owner, object_type, object_name, status, created, last_ddl_time in rows:
            try:
                parsed_type = ObjectType(object_type)
                parsed_status = OracleStatus(status)
            except ValueError:
                # The query restricts both columns, so this means the dictionary
                # returned something this build does not model. Skip loudly
                # rather than guess at a mapping.
                _log.warning(
                    "skipping %s.%s: unsupported type %r or status %r",
                    owner, object_name, object_type, status,
                    extra={"database": self.database.name},
                )
                continue

            key = (owner, object_type, object_name)
            if key in seen:
                continue
            seen.add(key)
            objects.append(
                OracleObject(
                    owner=owner,
                    object_type=parsed_type,
                    object_name=object_name,
                    database=self.database.name,
                    status=parsed_status,
                    created=created,
                    last_ddl_time=last_ddl_time,
                )
            )
        return objects
