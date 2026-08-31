"""Every SQL statement the application can issue (docs/architecture.md §3.4).

**All SQL lives here.** That is the point of the module: the read-only guard
test scans this one file for forbidden leading keywords, so a mutating
statement cannot be introduced anywhere in the project without failing CI.

Only three shapes are permitted: dictionary ``SELECT``s, read-only
``DBMS_METADATA`` calls, and session-scoped ``ALTER SESSION`` (decision D-2).

Configuration values are **never** interpolated. Where an ``IN`` list is needed
the *placeholder names* are generated (``:t0``, ``:t1``, …) and the values are
bound — the values themselves never touch the statement text.

**This module is not in the approved structure** (CLAUDE.md §3) — it is
mandated by architecture §3.4 but has no entry in §2. It needs owner
ratification; see the Phase 3 report.
"""

from __future__ import annotations

from typing import Any

from src.exceptions import ConnectionError as OracleConnectionError
from src.models.oracle_object import ObjectType

__all__ = [
    "ALLOWED_LEADING_KEYWORDS",
    "FORBIDDEN_KEYWORDS",
    "INTERNAL_SCHEMAS",
    "METADATA_TYPE_NAMES",
    "REQUIRED_VIEWS",
    "SELECT_DATABASE_TIME",
    "SELECT_METADATA_DDL",
    "SELECT_SOURCE",
    "SELECT_SYNONYM",
    "SELECT_TABLE_COLUMNS",
    "SELECT_VIEW_TEXT",
    "SESSION_INITIALIZATION",
    "TRANSFORM_PARAM_STATEMENTS",
    "assert_read_only",
    "build_discovery_query",
    "build_schema_list_query",
    "preflight_statement",
]

#: A statement may begin only with one of these (docs/architecture.md §3.4).
ALLOWED_LEADING_KEYWORDS: frozenset[str] = frozenset({"SELECT", "WITH", "BEGIN", "ALTER SESSION"})

#: Keywords that must never appear as a leading keyword in project SQL.
FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "TRUNCATE",
        "GRANT", "REVOKE", "COMMIT", "ROLLBACK", "EXECUTE IMMEDIATE",
    }
)


def assert_read_only(statement: str) -> None:
    """Reject any statement that is not read-only.

    The guard lives beside the statements it protects, so a mutating statement
    added to this module cannot slip past it.

    Args:
        statement: The SQL text about to be executed.

    Raises:
        src.exceptions.ConnectionError: If the statement is empty or its leading
            keyword is not in :data:`ALLOWED_LEADING_KEYWORDS`.
    """
    stripped = " ".join(statement.strip().split()).upper()
    if not stripped:
        raise OracleConnectionError("refusing to execute an empty statement")

    for allowed in ALLOWED_LEADING_KEYWORDS:
        if stripped.startswith(allowed + " ") or stripped == allowed:
            return

    leading = stripped.split(" ", 1)[0]
    raise OracleConnectionError(
        f"refusing to execute a non-read-only statement (leading keyword {leading!r})"
    )


#: Oracle-supplied schemas, excluded regardless of server version. On 12c+ the
#: ``ORACLE_MAINTAINED`` column does this properly; this list is what covers
#: pre-12c, where that column does not exist (docs/architecture.md §4.1).
INTERNAL_SCHEMAS: tuple[str, ...] = (
    "ANONYMOUS", "APPQOSSYS", "AUDSYS", "CTXSYS", "DBSFWUSER", "DBSNMP",
    "DIP", "DVF", "DVSYS", "EXFSYS", "GGSYS", "GSMADMIN_INTERNAL",
    "GSMCATUSER", "GSMROOTUSER", "GSMUSER", "LBACSYS", "MDDATA", "MDSYS",
    "OJVMSYS", "OLAPSYS", "ORACLE_OCM", "ORDDATA", "ORDPLUGINS", "ORDSYS",
    "OUTLN", "REMOTE_SCHEDULER_AGENT", "SI_INFORMTN_SCHEMA", "SYS", "SYS$UMF",
    "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC", "SYSTEM", "WMSYS", "XDB", "XS$NULL",
)

#: Applied once per session. Determinism: DDL text and dictionary output must
#: not vary with the runner's locale (docs/architecture.md §3.3).
SESSION_INITIALIZATION: tuple[str, ...] = (
    "ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'",
    "ALTER SESSION SET NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6'",
    "ALTER SESSION SET NLS_SORT = 'BINARY'",
    "ALTER SESSION SET NLS_COMP = 'BINARY'",
)

#: ``DBMS_METADATA`` transform parameters (docs/architecture.md §5.3). Rendered
#: as literal text from an internal constant table — no configuration value is
#: involved, and PL/SQL ``BOOLEAN`` cannot be bound portably.
TRANSFORM_PARAMS: dict[str, bool] = {
    "PRETTY": True,
    "SQLTERMINATOR": True,
    "STORAGE": False,
    "SEGMENT_ATTRIBUTES": False,
    "TABLESPACE": False,
    "EMIT_SCHEMA": True,
}

TRANSFORM_PARAM_STATEMENTS: tuple[str, ...] = tuple(
    "BEGIN DBMS_METADATA.SET_TRANSFORM_PARAM("
    f"DBMS_METADATA.SESSION_TRANSFORM, '{name}', {str(value).upper()}); END;"
    for name, value in TRANSFORM_PARAMS.items()
)

#: The watermark must come from the database clock, never the runner's (§4.2).
#:
#: The offset is selected alongside the timestamp because python-oracledb
#: returns ``SYSTIMESTAMP`` as a **naive** datetime — it drops the zone. Without
#: the offset the value is ambiguous, and a watermark stored in the wrong zone
#: silently skips every change in the intervening hours.
SELECT_DATABASE_TIME = "SELECT SYSTIMESTAMP, TO_CHAR(SYSTIMESTAMP, 'TZH:TZM') FROM dual"

#: Dictionary views the agent must be able to read (docs/architecture.md §21.2).
#: The pre-flight check reads each one so a missing grant is reported by name at
#: start-up, rather than failing obscurely part-way through a run.
REQUIRED_VIEWS: tuple[str, ...] = (
    "dba_objects", "dba_source", "dba_views", "dba_tab_columns", "dba_synonyms",
)


def preflight_statement(view: str) -> str:
    """Build the cheapest possible read against ``view``.

    Args:
        view: A name from :data:`REQUIRED_VIEWS` — a module constant, never
            configuration, so nothing user-supplied reaches the statement.

    Returns:
        A statement that touches at most one row.

    Raises:
        ValueError: If ``view`` is not a known required view.
    """
    if view not in REQUIRED_VIEWS:
        raise ValueError(f"{view!r} is not a required dictionary view")
    return f"SELECT 1 FROM {view} WHERE ROWNUM = 1"  # noqa: S608 - constant, not user input


#: PL/SQL source. ``ORDER BY line`` is mandatory — output order defines the file.
SELECT_SOURCE = (
    "SELECT text FROM dba_source "
    "WHERE owner = :owner AND name = :name AND type = :object_type "
    "ORDER BY line"
)

#: View text. Views are **not** in ``dba_source``; see the Phase 3 report.
SELECT_VIEW_TEXT = (
    "SELECT text FROM dba_views WHERE owner = :owner AND view_name = :name"
)

#: Table columns, ordered by ``column_id`` so the rendered DDL is stable.
SELECT_TABLE_COLUMNS = (
    "SELECT column_name, data_type, data_length, data_precision, data_scale, "
    "char_length, nullable, data_default "
    "FROM dba_tab_columns WHERE owner = :owner AND table_name = :name "
    "ORDER BY column_id"
)

SELECT_SYNONYM = (
    "SELECT table_owner, table_name, db_link FROM dba_synonyms "
    "WHERE owner = :owner AND synonym_name = :name"
)

#: Read-only metadata call, used for SEQUENCE and MATERIALIZED VIEW.
SELECT_METADATA_DDL = (
    "SELECT DBMS_METADATA.GET_DDL(:object_type, :name, :owner) FROM dual"
)

#: ``DBMS_METADATA`` spells some types differently from ``DBA_OBJECTS``:
#: it wants ``MATERIALIZED_VIEW`` where the dictionary says ``MATERIALIZED VIEW``.
METADATA_TYPE_NAMES: dict[ObjectType, str] = {
    ObjectType.SEQUENCE: "SEQUENCE",
    ObjectType.MATERIALIZED_VIEW: "MATERIALIZED_VIEW",
}

_DISCOVERY_COLUMNS = (
    "owner, object_type, object_name, status, created, last_ddl_time"
)

#: Predicates that apply to every discovery and inventory query (§4.1). These
#: are static text: no configuration value appears in any of them.
_STATIC_PREDICATES = (
    "status IN ('VALID', 'INVALID')",
    "generated = 'N'",
    "secondary = 'N'",
    "subobject_name IS NULL",
    "object_name NOT LIKE 'BIN$%'",
)


def _placeholders(prefix: str, values: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    """Render an ``IN`` list as generated placeholder names plus their binds.

    Args:
        prefix: Placeholder prefix, e.g. ``t`` for ``:t0, :t1``.
        values: The values to bind.

    Returns:
        The comma-separated placeholder text and the bind mapping.
    """
    names = [f"{prefix}{index}" for index in range(len(values))]
    return ", ".join(f":{name}" for name in names), dict(zip(names, values, strict=True))


def _scope_predicates(
    *,
    object_types: tuple[str, ...],
    include_schemas: tuple[str, ...],
    exclude_schemas: tuple[str, ...],
    oracle_maintained_column: bool,
) -> tuple[list[str], dict[str, Any]]:
    """Assemble the predicates that define "in scope", shared by every query.

    Discovery, the reconciliation inventory, and the schema list must agree
    exactly on what is in scope: a schema the list query omits is never
    processed, and one the inventory omits has its files deleted. Deriving all
    three from this one helper is what keeps them from drifting apart.

    Args:
        object_types: ``DBA_OBJECTS.OBJECT_TYPE`` values in scope.
        include_schemas: Owners to restrict to; empty means "all not excluded".
        exclude_schemas: Owners to exclude, on top of :data:`INTERNAL_SCHEMAS`.
        oracle_maintained_column: Whether the server has the 12c+
            ``ORACLE_MAINTAINED`` column.

    Returns:
        The predicate fragments and their bind mapping.

    Raises:
        ValueError: If ``object_types`` is empty, which would match nothing and
            almost certainly indicates a configuration bug.
    """
    if not object_types:
        raise ValueError("at least one object type must be in scope")

    binds: dict[str, Any] = {}
    predicates = list(_STATIC_PREDICATES)

    types_text, type_binds = _placeholders("t", object_types)
    predicates.insert(0, f"object_type IN ({types_text})")
    binds.update(type_binds)

    excluded = tuple(dict.fromkeys(INTERNAL_SCHEMAS + exclude_schemas))
    exclude_text, exclude_binds = _placeholders("x", excluded)
    predicates.append(f"owner NOT IN ({exclude_text})")
    binds.update(exclude_binds)

    if oracle_maintained_column:
        predicates.append("oracle_maintained = 'N'")

    if include_schemas:
        include_text, include_binds = _placeholders("i", include_schemas)
        predicates.append(f"owner IN ({include_text})")
        binds.update(include_binds)

    return predicates, binds


def build_schema_list_query(
    *,
    object_types: tuple[str, ...],
    include_schemas: tuple[str, ...] = (),
    exclude_schemas: tuple[str, ...] = (),
    oracle_maintained_column: bool,
) -> tuple[str, dict[str, Any]]:
    """Assemble the query that resolves which schemas are in scope.

    Needed because the unit of work is the schema (decision D-15) and an empty
    ``include`` filter means "every schema not excluded" — which cannot be
    enumerated without asking the database. Never incremental: the schema list
    must not depend on a watermark, or a schema with no recent DDL would
    silently drop out of scope and have its files reconciled away.

    Args:
        object_types: ``DBA_OBJECTS.OBJECT_TYPE`` values in scope.
        include_schemas: Owners to restrict to; empty means "all not excluded".
        exclude_schemas: Owners to exclude, on top of :data:`INTERNAL_SCHEMAS`.
        oracle_maintained_column: Whether the server has the 12c+
            ``ORACLE_MAINTAINED`` column.

    Returns:
        The statement text and the bind mapping.

    Raises:
        ValueError: If ``object_types`` is empty.
    """
    predicates, binds = _scope_predicates(
        object_types=object_types,
        include_schemas=include_schemas,
        exclude_schemas=exclude_schemas,
        oracle_maintained_column=oracle_maintained_column,
    )
    statement = (
        "SELECT DISTINCT owner FROM dba_objects WHERE "  # noqa: S608 - placeholders only
        + " AND ".join(predicates)
        + " ORDER BY owner"
    )
    return statement, binds


def build_discovery_query(
    *,
    object_types: tuple[str, ...],
    include_schemas: tuple[str, ...] = (),
    exclude_schemas: tuple[str, ...] = (),
    incremental: bool,
    oracle_maintained_column: bool,
) -> tuple[str, dict[str, Any]]:
    """Assemble the ``DBA_OBJECTS`` query and its bind values.

    The same builder produces both the incremental discovery query and the
    reconciliation inventory query; the only difference is the
    ``LAST_DDL_TIME`` predicate, which is omitted entirely on a full export
    (docs/architecture.md §4.1, §9.1).

    Args:
        object_types: ``DBA_OBJECTS.OBJECT_TYPE`` values in scope.
        include_schemas: Owners to restrict to; empty means "all not excluded".
        exclude_schemas: Owners to exclude, on top of :data:`INTERNAL_SCHEMAS`.
        incremental: Whether to add ``last_ddl_time > :watermark``. The caller
            binds ``watermark`` when this is ``True``.
        oracle_maintained_column: Whether the server has the 12c+
            ``ORACLE_MAINTAINED`` column.

    Returns:
        The statement text and the bind mapping (without ``watermark``).

    Raises:
        ValueError: If ``object_types`` is empty, which would match nothing and
            almost certainly indicates a configuration bug.
    """
    predicates, binds = _scope_predicates(
        object_types=object_types,
        include_schemas=include_schemas,
        exclude_schemas=exclude_schemas,
        oracle_maintained_column=oracle_maintained_column,
    )

    if incremental:
        predicates.append("last_ddl_time > :watermark")

    # S608 is suppressed deliberately, and narrowly. Every fragment assembled
    # here is either a module constant or a generated *placeholder name*
    # (":t0", ":i0", ":x0"); no configuration value, schema name, object type,
    # or watermark ever reaches the statement text — they are all bound. The
    # test `test_discovery_query_binds_every_configuration_value` asserts
    # exactly that by searching the rendered SQL for each input value.
    statement = (
        f"SELECT {_DISCOVERY_COLUMNS} FROM dba_objects WHERE "  # noqa: S608
        + " AND ".join(predicates)
        + " ORDER BY owner, object_type, object_name"
    )
    return statement, binds
