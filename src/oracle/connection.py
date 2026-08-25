"""Connection pooling, session initialization, and the read-only guard.

See docs/architecture.md §3. One pool per database, created at run-unit start
and closed in a ``finally`` block. Thin mode is the default, so no Oracle
Instant Client is needed.

Read-only enforcement has three independent layers (§3.4): the account's
privileges (the real guarantee), :func:`assert_read_only` on every statement,
and the CI test that scans :mod:`src.oracle.sql`. :func:`preflight` checks the
first of those up front, so a missing grant is reported by name at start-up
rather than failing obscurely part-way through a run (§21.2).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Protocol

import oracledb

from src.config.models import DatabaseConfig, Settings
from src.config.secrets import EnvironmentSecretProvider, SecretProvider
from src.exceptions import ConnectionError as OracleConnectionError
from src.logging.logger import get_logger
from src.oracle import sql
from src.utils.retry import RetryPolicy, retry_call
from src.utils.timers import parse_utc_offset

__all__ = [
    "ALLOWED_LEADING_KEYWORDS",
    "FORBIDDEN_KEYWORDS",
    "TRANSIENT_ORACLE_CODES",
    "ConnectionFactory",
    "OracleSession",
    "Session",
    "assert_read_only",
    "is_transient",
    "preflight",
]

_log = get_logger(__name__)

# The read-only guard lives in src.oracle.sql, beside the statements it
# protects. Re-exported here because it is part of this layer's interface.
ALLOWED_LEADING_KEYWORDS = sql.ALLOWED_LEADING_KEYWORDS
FORBIDDEN_KEYWORDS = sql.FORBIDDEN_KEYWORDS
assert_read_only = sql.assert_read_only

# Errors worth retrying: a dropped session (28, 3113, 3114, 3135, 25408), an
# instance starting or stopping (1033, 1034, 1089), or a listener/network fault
# (the 12xxx range). Everything else — a missing privilege, a vanished object,
# a wrapped source — fails identically on every attempt and is never retried.
TRANSIENT_ORACLE_CODES: frozenset[int] = frozenset(
    {28, 1033, 1034, 1089, 3113, 3114, 3135,
     12152, 12170, 12514, 12528, 12537, 12541, 12545, 12570, 25408}
)

#: Thin-mode driver errors that mean "could not reach the database".
_TRANSIENT_DRIVER_PREFIXES = ("DPY-4011", "DPY-6005", "DPY-3010", "DPI-1080")


def preflight(session: Session) -> list[str]:
    """Return the required views this session cannot read; empty means all grants are present."""
    missing = []
    for view in sql.REQUIRED_VIEWS:
        try:
            session.fetch_all(sql.preflight_statement(view))
        except Exception:
            missing.append(view)
    return missing


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is a connectivity-class error worth retrying.

    Args:
        exc: The exception raised by a database call.

    Returns:
        ``True`` for a dropped session, an unavailable listener, or a network
        fault; ``False`` for anything deterministic.
    """
    if isinstance(exc, OracleConnectionError):
        return True
    if not isinstance(exc, oracledb.Error):
        return False

    error = exc.args[0] if exc.args else None
    code = getattr(error, "code", None)
    if isinstance(code, int) and code in TRANSIENT_ORACLE_CODES:
        return True
    message = getattr(error, "full_code", "") or str(error)
    return message.startswith(_TRANSIENT_DRIVER_PREFIXES)


class Session(Protocol):
    """The read-only database operations the domain layer needs.

    Narrow on purpose: tests substitute a fake implementing these three members
    and exercise discovery and extraction with no Oracle instance.
    """

    @property
    def server_major_version(self) -> int:
        """The server's major version, e.g. ``19``."""
        ...

    def fetch_all(self, statement: str, binds: dict[str, Any] | None = None) -> list[tuple]:
        """Run a query and return every row."""
        ...

    def fetch_value(self, statement: str, binds: dict[str, Any] | None = None) -> Any:
        """Run a query and return the first column of the first row, or ``None``."""
        ...

    def database_time(self) -> datetime:
        """Read the database clock, for use as the candidate watermark."""
        ...


class OracleSession:
    """A pooled connection, initialized for deterministic read-only access.

    Every ``fetch_*`` method takes the SQL text and an optional bind mapping,
    passes the statement through :func:`assert_read_only` first, and raises
    :class:`src.exceptions.ConnectionError` if it is not read-only.
    """

    def __init__(self, connection: oracledb.Connection, settings: Settings) -> None:
        """Wrap a pooled connection; ``settings`` supplies the fetch tuning."""
        self._connection = connection
        self._settings = settings

    @property
    def server_major_version(self) -> int:
        """The server's major version, or ``0`` when it cannot be determined."""
        raw = (self._connection.version or "").split(".", 1)[0]
        return int(raw) if raw.isdigit() else 0

    def initialize(self) -> None:
        """Apply NLS settings and ``DBMS_METADATA`` transform parameters.

        Session-scoped only: nothing in the database is changed and no object is
        created, so this does not violate the read-only rule (decision D-2).
        """
        for statement in sql.SESSION_INITIALIZATION + sql.TRANSFORM_PARAM_STATEMENTS:
            self._execute(statement, None).close()

    def fetch_all(self, statement: str, binds: dict[str, Any] | None = None) -> list[tuple]:
        """Return every row, in the order the database returned them."""
        cursor = self._execute(statement, binds)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def fetch_value(self, statement: str, binds: dict[str, Any] | None = None) -> Any:
        """Return the first column of the first row, or ``None`` if there is none.

        A CLOB is read in full, never truncated (docs/architecture.md §5.3).
        """
        cursor = self._execute(statement, binds)
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            return None
        return row[0].read() if hasattr(row[0], "read") else row[0]

    def database_time(self) -> datetime:
        """Read the database clock as a timezone-aware datetime.

        Never the runner's clock: skew between them causes missed changes
        (docs/architecture.md §4.2). The driver returns ``SYSTIMESTAMP`` naive,
        so the server's offset is read alongside it and attached here — the
        resulting ``tzinfo`` is the *server's*, which is what converts a stored
        watermark back into the frame ``LAST_DDL_TIME`` is recorded in.
        """
        naive, offset = self.fetch_all(sql.SELECT_DATABASE_TIME)[0]
        if naive.tzinfo is not None:
            return naive
        return naive.replace(tzinfo=parse_utc_offset(offset))

    def _execute(self, statement: str, binds: dict[str, Any] | None) -> oracledb.Cursor:
        """Guard, then execute, returning the open cursor."""
        assert_read_only(statement)
        cursor = self._connection.cursor()
        cursor.arraysize = self._settings.arraysize
        cursor.prefetchrows = self._settings.prefetchrows
        cursor.execute(statement, binds or {})
        return cursor


class ConnectionFactory:
    """Creates and tears down one connection pool per database."""

    def __init__(
        self, settings: Settings, secret_provider: SecretProvider | None = None
    ) -> None:
        """Create a factory; ``secret_provider`` resolves the DSN, user, and password."""
        self.settings = settings
        self.secret_provider: SecretProvider = secret_provider or EnvironmentSecretProvider()
        self._pools: dict[str, oracledb.ConnectionPool] = {}

    @contextmanager
    def session(self, database: DatabaseConfig) -> Iterator[OracleSession]:
        """Yield an initialized read-only session for ``database``.

        The retry budget is applied here rather than around pool creation: in
        thin mode ``create_pool`` is lazy, so the connection is actually
        established at ``acquire`` and a retry around creation would never fire.

        Args:
            database: The database to connect to.

        Yields:
            An initialized session.

        Raises:
            src.exceptions.ConnectionError: If a connection cannot be
                established within the retry budget.
        """
        pool = self._pool_for(database)
        retry = self.settings.retry
        policy = RetryPolicy(
            attempts=retry.connect_attempts,
            base_delay=retry.connect_backoff_seconds,
            jitter_ratio=retry.jitter_ratio,
        )

        try:
            connection = retry_call(
                lambda: self._connect(pool),
                policy=policy,
                is_retryable=is_transient,
                description=f"connect to {database.name}",
                on_retry=lambda attempt, delay, exc: _log.warning(
                    "connect attempt %d failed (%s); retrying in %.1fs",
                    attempt, exc, delay, extra={"database": database.name},
                ),
            )
        except Exception as exc:
            raise OracleConnectionError(f"{database.name}: cannot connect: {exc}") from exc

        try:
            yield OracleSession(connection, self.settings)
        finally:
            connection.close()

    def _connect(self, pool: oracledb.ConnectionPool) -> oracledb.Connection:
        """Acquire and initialize one connection, releasing it if setup fails."""
        connection = pool.acquire()
        try:
            OracleSession(connection, self.settings).initialize()
        except BaseException:
            connection.close()
            raise
        return connection

    def _pool_for(self, database: DatabaseConfig) -> oracledb.ConnectionPool:
        """Return the pool for ``database``, creating it on first use.

        Credentials are resolved at call time and never stored on the factory.

        Raises:
            src.exceptions.ConnectionError: If the pool cannot be created.
        """
        if database.name in self._pools:
            return self._pools[database.name]
        if database.thick_mode:
            oracledb.init_oracle_client()
        try:
            pool = oracledb.create_pool(
                dsn=self.secret_provider.resolve(database.dsn_env),
                user=self.secret_provider.resolve(database.username_env),
                password=self.secret_provider.resolve(database.password_env),
                min=1,
                max=2,
                increment=1,
                ping_interval=60,
                getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
                wait_timeout=30_000,
            )
        except oracledb.Error as exc:
            raise OracleConnectionError(f"{database.name}: cannot create a pool: {exc}") from exc

        self._pools[database.name] = pool
        return pool

    def close(self) -> None:
        """Close every pool this factory created."""
        for name, pool in self._pools.items():
            try:
                pool.close(force=True)
            except oracledb.Error as exc:
                _log.warning("could not close pool: %s", exc, extra={"database": name})
        self._pools.clear()
