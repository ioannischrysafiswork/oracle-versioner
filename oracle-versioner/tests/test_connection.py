"""Oracle connection layer: the read-only guard, error classification, retry.

The read-only guard test is **blocking in CI** (CLAUDE.md §6). It has two
halves: the guard itself rejects mutating statements, and a scan of every SQL
constant in :mod:`src.oracle.sql` proves nothing mutating was ever added.

Everything here runs with no Oracle instance.
"""

from __future__ import annotations

import pytest

from src.exceptions import ConnectionError as OracleConnectionError
from src.oracle import sql
from src.oracle.connection import (
    ALLOWED_LEADING_KEYWORDS,
    FORBIDDEN_KEYWORDS,
    TRANSIENT_ORACLE_CODES,
    assert_read_only,
    is_transient,
)


def _sql_constants() -> list[tuple[str, str]]:
    """Every SQL string constant defined in src.oracle.sql."""
    found: list[tuple[str, str]] = []
    for name in dir(sql):
        if name.startswith("_"):
            continue
        value = getattr(sql, name)
        if isinstance(value, str) and " " in value:
            found.append((name, value))
        elif isinstance(value, tuple) and all(isinstance(v, str) for v in value):
            found.extend((f"{name}[{i}]", v) for i, v in enumerate(value) if " " in v)
    return found


# --------------------------------------------------------------------------- the guard


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT owner FROM dba_objects WHERE owner = :owner",
        "  select 1 from dual  ",
        "WITH t AS (SELECT 1 FROM dual) SELECT * FROM t",
        "BEGIN dbms_metadata.set_transform_param(:h, 'PRETTY', TRUE); END;",
        "ALTER SESSION SET nls_sort = 'BINARY'",
        "alter   session  set nls_comp = 'BINARY'",
    ],
)
def test_read_only_statements_are_permitted(statement: str) -> None:
    """Dictionary reads, metadata calls, and ALTER SESSION are allowed."""
    assert_read_only(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET c = 1",
        "DELETE FROM t",
        "MERGE INTO t USING s ON (1=1)",
        "CREATE TABLE t (c NUMBER)",
        "DROP TABLE t",
        "TRUNCATE TABLE t",
        "GRANT SELECT ON t TO u",
        "REVOKE SELECT ON t FROM u",
        "ALTER TABLE t ADD (c NUMBER)",
        "COMMIT",
        "",
    ],
)
def test_mutating_statements_are_rejected(statement: str) -> None:
    """Anything that could write to the source database is refused."""
    with pytest.raises(OracleConnectionError):
        assert_read_only(statement)


def test_alter_session_is_allowed_but_bare_alter_is_not() -> None:
    """Decision D-2 permits ALTER SESSION only — not ALTER of anything else."""
    assert_read_only("ALTER SESSION SET nls_date_format = 'YYYY-MM-DD'")
    with pytest.raises(OracleConnectionError):
        assert_read_only("ALTER PACKAGE hr.p COMPILE")


def test_guard_keyword_sets_are_disjoint() -> None:
    """The allow-list and the deny-list can never both match a statement."""
    assert not ALLOWED_LEADING_KEYWORDS & FORBIDDEN_KEYWORDS


# --------------------------------------------------------------------------- SQL audit


def test_sql_module_defines_statements() -> None:
    """The scan below is meaningless if it finds nothing to scan."""
    assert len(_sql_constants()) >= 10


@pytest.mark.parametrize("name,statement", _sql_constants())
def test_every_sql_constant_is_read_only(name: str, statement: str) -> None:
    """BLOCKING: no statement in the project may mutate the source database."""
    assert_read_only(statement)


@pytest.mark.parametrize("name,statement", _sql_constants())
def test_no_sql_constant_contains_a_forbidden_keyword(name: str, statement: str) -> None:
    """BLOCKING: a mutating keyword must not appear anywhere in a statement.

    ``CREATE`` is exempt only where it is the *rendered output* of extraction
    (``CREATE TABLE`` text built for a .sql file), never something executed.
    """
    upper = " ".join(statement.upper().split())
    for keyword in FORBIDDEN_KEYWORDS:
        assert keyword not in upper, f"{name} contains {keyword}"


def test_discovery_query_binds_every_configuration_value() -> None:
    """Configuration values are bound, never interpolated (CLAUDE.md §7.3)."""
    statement, binds = sql.build_discovery_query(
        object_types=("PACKAGE", "VIEW"),
        include_schemas=("HR",),
        exclude_schemas=("SCRATCH",),
        incremental=True,
        oracle_maintained_column=True,
    )
    for value in ("PACKAGE", "VIEW", "HR", "SCRATCH"):
        assert value not in statement
    assert set(binds.values()) >= {"PACKAGE", "VIEW", "HR", "SCRATCH"}
    assert_read_only(statement)


# --------------------------------------------------------------------------- classification


class _DriverError:
    """Stands in for the error object oracledb attaches as ``args[0]``."""

    def __init__(self, code: int = 0, full_code: str = "") -> None:
        self.code = code
        self.full_code = full_code

    def __str__(self) -> str:
        return self.full_code or f"ORA-{self.code:05d}"


def _oracle_error(code: int = 0, full_code: str = "") -> Exception:
    import oracledb

    return oracledb.DatabaseError(_DriverError(code, full_code))


@pytest.mark.parametrize("code", sorted(TRANSIENT_ORACLE_CODES))
def test_connectivity_errors_are_transient(code: int) -> None:
    """A dropped session or an unavailable listener is worth retrying."""
    assert is_transient(_oracle_error(code))


@pytest.mark.parametrize("code", [942, 1031, 31603, 24344])
def test_logical_errors_are_not_transient(code: int) -> None:
    """A missing privilege or a vanished object fails identically every time."""
    assert not is_transient(_oracle_error(code))


@pytest.mark.parametrize("full_code", ["DPY-4011", "DPY-6005"])
def test_thin_mode_connectivity_errors_are_transient(full_code: str) -> None:
    """The thin driver reports lost connections with its own error codes."""
    assert is_transient(_oracle_error(full_code=full_code))


def test_project_connection_errors_are_transient() -> None:
    """Our own ConnectionError means "could not reach the database"."""
    assert is_transient(OracleConnectionError("no listener"))


def test_unrelated_exceptions_are_not_transient() -> None:
    """A programming error must never be retried."""
    assert not is_transient(ValueError("bug"))


# --------------------------------------------------------------------------- session init


def test_session_initialization_is_read_only() -> None:
    """Every session-setup statement passes the guard."""
    for statement in sql.SESSION_INITIALIZATION + sql.TRANSFORM_PARAM_STATEMENTS:
        assert_read_only(statement)


def test_transform_parameters_suppress_storage_at_the_source() -> None:
    """Storage noise is removed in Oracle, not by regular expressions."""
    rendered = " ".join(sql.TRANSFORM_PARAM_STATEMENTS)
    assert "'STORAGE', FALSE" in rendered
    assert "'SEGMENT_ATTRIBUTES', FALSE" in rendered
    assert "'TABLESPACE', FALSE" in rendered
    assert "'PRETTY', TRUE" in rendered
    assert "'SQLTERMINATOR', TRUE" in rendered


def test_nls_is_pinned_for_determinism() -> None:
    """Normalized output must not vary with the runner's locale."""
    rendered = " ".join(sql.SESSION_INITIALIZATION)
    assert "NLS_SORT = 'BINARY'" in rendered
    assert "NLS_COMP = 'BINARY'" in rendered
    assert "NLS_DATE_FORMAT" in rendered
