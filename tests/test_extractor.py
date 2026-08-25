"""Object discovery and DDL extraction, against fake cursors.

No Oracle instance: :class:`tests.conftest.FakeSession` scripts the rows each
query returns, which is enough to assert query predicates, bind names, line
ordering, and the validation gate.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.config.models import DatabaseConfig, ObjectTypeFilter, SchemaFilter, Settings
from src.exceptions import DiscoveryError, ExtractionError
from src.models.database import DatabaseRuntime
from src.models.oracle_object import ExtractedSource, ObjectType, OracleObject
from src.oracle import sql
from src.oracle.ddl_extractor import MetadataStrategy
from src.oracle.metadata import STRATEGY_BY_TYPE, ExtractionDispatcher, ObjectVanished
from src.oracle.object_discovery import ObjectDiscovery
from src.oracle.source_extractor import (
    ColumnStrategy,
    DictionaryStrategy,
    SourceStrategy,
    ViewStrategy,
)
from tests.conftest import FakeSession

DB = DatabaseConfig(
    name="TESTDB", git_folder="TESTDB",
    dsn_env="D", username_env="U", password_env="P",
)
RUNTIME_FULL = DatabaseRuntime(name="TESTDB", git_folder=Path("TESTDB"), watermark=None)
RUNTIME_INCREMENTAL = DatabaseRuntime(
    name="TESTDB", git_folder=Path("TESTDB"),
    watermark=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
)


def _obj(object_type: ObjectType, name: str = "EMPLOYEE_PKG") -> OracleObject:
    return OracleObject(
        owner="HR", object_type=object_type, object_name=name, database="TESTDB"
    )


# --------------------------------------------------------------------------- dispatch table


def test_every_object_type_has_a_strategy() -> None:
    """The dispatch table is total over the supported types."""
    assert set(STRATEGY_BY_TYPE) == set(ObjectType)


@pytest.mark.parametrize(
    ("object_type", "strategy"),
    [
        (ObjectType.PACKAGE, SourceStrategy),
        (ObjectType.PACKAGE_BODY, SourceStrategy),
        (ObjectType.PROCEDURE, SourceStrategy),
        (ObjectType.FUNCTION, SourceStrategy),
        (ObjectType.TYPE, SourceStrategy),
        (ObjectType.TYPE_BODY, SourceStrategy),
        (ObjectType.TRIGGER, SourceStrategy),
        (ObjectType.VIEW, ViewStrategy),
        (ObjectType.TABLE, ColumnStrategy),
        (ObjectType.SEQUENCE, MetadataStrategy),
        (ObjectType.SYNONYM, DictionaryStrategy),
        (ObjectType.MATERIALIZED_VIEW, MetadataStrategy),
    ],
)
def test_strategy_matches_the_specification(object_type: ObjectType, strategy: type) -> None:
    """Type-to-mechanism mapping follows docs/requirements.md §6.

    The one departure is VIEW: Oracle keeps view text in DBA_VIEWS, not
    DBA_SOURCE, so ViewStrategy is used. See the Phase 3 report.
    """
    assert STRATEGY_BY_TYPE[object_type] is strategy


def test_materialized_view_uses_the_dbms_metadata_spelling() -> None:
    """DBMS_METADATA wants MATERIALIZED_VIEW where the dictionary says a space."""
    assert sql.METADATA_TYPE_NAMES[ObjectType.MATERIALIZED_VIEW] == "MATERIALIZED_VIEW"
    assert sql.METADATA_TYPE_NAMES[ObjectType.SEQUENCE] == "SEQUENCE"


# --------------------------------------------------------------------------- discovery query


def _discovery_statement(**overrides) -> str:
    kwargs = {
        "object_types": ("PACKAGE",),
        "incremental": True,
        "oracle_maintained_column": True,
    }
    kwargs.update(overrides)
    return sql.build_discovery_query(**kwargs)[0]


@pytest.mark.parametrize(
    "predicate",
    [
        "status IN ('VALID', 'INVALID')",
        "generated = 'N'",
        "secondary = 'N'",
        "subobject_name IS NULL",
        "object_name NOT LIKE 'BIN$%'",
        "owner NOT IN (",
        "ORDER BY owner, object_type, object_name",
    ],
)
def test_discovery_query_carries_the_required_predicates(predicate: str) -> None:
    """Every predicate in docs/architecture.md §4.1 is present."""
    assert predicate in _discovery_statement()


def test_incremental_query_binds_the_watermark() -> None:
    """The change-detection criterion is a bind, not interpolated text."""
    assert "last_ddl_time > :watermark" in _discovery_statement(incremental=True)


def test_full_export_omits_the_watermark_predicate() -> None:
    """With no watermark the predicate is dropped entirely, not defaulted.

    ``last_ddl_time`` still appears in the SELECT list; it is the *predicate*
    that must be absent.
    """
    assert "last_ddl_time > :watermark" not in _discovery_statement(incremental=False)


def test_oracle_maintained_is_omitted_before_12c() -> None:
    """The column does not exist pre-12c; the deny-list covers those servers."""
    assert "oracle_maintained" not in _discovery_statement(oracle_maintained_column=False)
    assert "oracle_maintained = 'N'" in _discovery_statement(oracle_maintained_column=True)


def test_internal_schemas_are_always_excluded() -> None:
    """SYS and friends are excluded even with no configured exclude list."""
    _, binds = sql.build_discovery_query(
        object_types=("PACKAGE",), incremental=False, oracle_maintained_column=False
    )
    assert {"SYS", "SYSTEM", "XDB"} <= set(binds.values())


def test_configured_excludes_extend_the_internal_list() -> None:
    """A configured exclusion is added to, not substituted for, the deny-list."""
    _, binds = sql.build_discovery_query(
        object_types=("PACKAGE",), exclude_schemas=("SCRATCH",),
        incremental=False, oracle_maintained_column=False,
    )
    assert {"SYS", "SCRATCH"} <= set(binds.values())


def test_include_filter_is_applied_in_sql() -> None:
    """Schema filters are pushed into the query, not applied in Python."""
    statement, binds = sql.build_discovery_query(
        object_types=("PACKAGE",), include_schemas=("HR", "PAYROLL"),
        incremental=False, oracle_maintained_column=False,
    )
    assert "owner IN (:i0, :i1)" in statement
    assert binds["i0"] == "HR"


def test_empty_object_type_list_is_rejected() -> None:
    """A query matching nothing is a configuration bug, not a valid run."""
    with pytest.raises(ValueError, match="at least one object type"):
        sql.build_discovery_query(
            object_types=(), incremental=False, oracle_maintained_column=False
        )


# --------------------------------------------------------------------------- discovery run


def _discovery(session: FakeSession, database: DatabaseConfig = DB) -> ObjectDiscovery:
    return ObjectDiscovery(session, database, Settings())


def test_discovery_maps_rows_to_objects() -> None:
    """Rows become ordered value objects carrying their database name."""
    session = FakeSession({"dba_objects": [
        ("HR", "PACKAGE", "P1", "VALID", None, None),
        ("HR", "VIEW", "V1", "INVALID", None, None),
    ]})
    objects = _discovery(session).discover_changed(RUNTIME_FULL)
    assert [(o.owner, str(o.object_type), o.object_name) for o in objects] == [
        ("HR", "PACKAGE", "P1"), ("HR", "VIEW", "V1")
    ]
    assert all(o.database == "TESTDB" for o in objects)


def test_discovery_deduplicates_by_owner_type_name() -> None:
    """No object is extracted twice in one execution (§5.6)."""
    session = FakeSession({"dba_objects": [
        ("HR", "PACKAGE", "P1", "VALID", None, None),
        ("HR", "PACKAGE", "P1", "VALID", None, None),
    ]})
    assert len(_discovery(session).discover_changed(RUNTIME_FULL)) == 1


def test_discovery_skips_unmodelled_types_loudly() -> None:
    """An unexpected dictionary value is skipped, never guessed at."""
    session = FakeSession({"dba_objects": [
        ("HR", "JAVA CLASS", "J", "VALID", None, None),
        ("HR", "PACKAGE", "P1", "VALID", None, None),
    ]})
    assert len(_discovery(session).discover_changed(RUNTIME_FULL)) == 1


def _discovery_call(session: FakeSession) -> tuple[str, dict]:
    """The discovery query itself, ignoring the clock and pre-flight probes."""
    return next(call for call in session.executed if "owner, object_type" in call[0])


def test_discovery_binds_the_watermark_as_server_local_naive() -> None:
    """`LAST_DDL_TIME` is a DATE in server-local time, so the bind must match.

    Binding a timezone-aware value makes Oracle convert it using the SESSION
    timezone — the *client's* — which against a server in another zone silently
    skips every change in the intervening hours.
    """
    session = FakeSession()  # server at UTC
    _discovery(session).discover_changed(RUNTIME_INCREMENTAL)
    _, binds = _discovery_call(session)

    assert binds["watermark"] == datetime(2026, 8, 1, 9, 0)  # naive
    assert binds["watermark"].tzinfo is None


def test_watermark_is_converted_into_the_servers_zone() -> None:
    """A server in +03:00 must receive 12:00, not the stored 09:00 UTC.

    Regression: found against a real instance, where the driver returned
    SYSTIMESTAMP naive and SESSIONTIMEZONE was the client's +03:00 while
    DBTIMEZONE was +00:00.
    """
    session = FakeSession()
    session.server_timezone = timezone(timedelta(hours=3))
    _discovery(session).discover_changed(RUNTIME_INCREMENTAL)
    _, binds = _discovery_call(session)

    assert binds["watermark"] == datetime(2026, 8, 1, 12, 0)
    assert binds["watermark"].tzinfo is None


def test_inventory_omits_the_watermark() -> None:
    """Reconciliation reads the full in-scope list, metadata only."""
    session = FakeSession()
    _discovery(session).full_inventory()
    statement, binds = session.executed[0]
    assert "last_ddl_time > :watermark" not in statement
    assert "watermark" not in binds


def test_discovery_respects_the_configured_object_types() -> None:
    """Only configured types are bound into the query."""
    database = replace(DB, object_types=ObjectTypeFilter(types=(ObjectType.VIEW,)))
    session = FakeSession()
    _discovery(session, database).full_inventory()
    _, binds = session.executed[0]
    assert [v for k, v in binds.items() if k.startswith("t")] == ["VIEW"]


def test_discovery_respects_schema_filters() -> None:
    """Include and exclude filters both reach the query."""
    database = replace(DB, schemas=SchemaFilter(include=("HR",), exclude=("SCRATCH",)))
    session = FakeSession()
    _discovery(session, database).full_inventory()
    _, binds = session.executed[0]
    assert "HR" in binds.values()
    assert "SCRATCH" in binds.values()


def test_discovery_failure_becomes_a_database_error() -> None:
    """A failed discovery ends this database, and only this database."""
    session = FakeSession()
    session.failures["dba_objects"] = RuntimeError("ORA-00942")
    with pytest.raises(DiscoveryError):
        _discovery(session).full_inventory()


# --------------------------------------------------------------------------- extraction


def test_source_lines_are_joined_in_retrieved_order() -> None:
    """Line order is the file; it is never reordered or trimmed here."""
    session = FakeSession({"dba_source": [
        ("PACKAGE BODY employee_pkg AS\n",),
        ("  PROCEDURE hire IS\n",),
        ("  BEGIN NULL; END;\n",),
        ("END employee_pkg;\n",),
    ]})
    source = SourceStrategy(session).extract(_obj(ObjectType.PACKAGE_BODY))
    assert source.text == (
        "CREATE OR REPLACE PACKAGE BODY employee_pkg AS\n"
        "  PROCEDURE hire IS\n  BEGIN NULL; END;\nEND employee_pkg;\n"
    )


def test_source_extraction_orders_by_line() -> None:
    """ORDER BY LINE is mandatory wherever output order defines a file."""
    session = FakeSession()
    SourceStrategy(session).extract(_obj(ObjectType.PACKAGE))
    statement, binds = session.executed[0]
    assert statement.rstrip().endswith("ORDER BY line")
    assert binds == {"owner": "HR", "name": "EMPLOYEE_PKG", "object_type": "PACKAGE"}


def test_long_source_is_not_truncated() -> None:
    """A large package body survives intact."""
    lines = [(f"-- line {n}\n",) for n in range(5000)]
    session = FakeSession({"dba_source": lines})
    source = SourceStrategy(session).extract(_obj(ObjectType.PACKAGE_BODY))
    assert source.text.count("\n") == 5000
    assert "-- line 4999" in source.text


def test_view_is_extracted_from_dba_views() -> None:
    """Views live in DBA_VIEWS; DBA_SOURCE would return nothing."""
    session = FakeSession({"dba_views": [("SELECT 1 FROM dual",)]})
    source = ViewStrategy(session).extract(_obj(ObjectType.VIEW, "V_EMP"))
    assert source.text == 'CREATE OR REPLACE VIEW "HR"."V_EMP" AS\nSELECT 1 FROM dual\n'
    assert "dba_views" in session.executed[0][0]


def test_table_columns_render_in_column_id_order() -> None:
    """A table is rendered from DBA_TAB_COLUMNS, ordered by COLUMN_ID."""
    session = FakeSession({"dba_tab_columns": [
        ("ID", "NUMBER", 22, 10, 0, None, "N", None),
        ("NAME", "VARCHAR2", 100, None, None, 100, "Y", None),
        ("STATUS", "VARCHAR2", 10, None, None, 10, "N", "'NEW'"),
    ]})
    text = ColumnStrategy(session).extract(_obj(ObjectType.TABLE, "EMPLOYEES")).text
    assert text == (
        'CREATE TABLE "HR"."EMPLOYEES" (\n'
        '  "ID" NUMBER(10) NOT NULL,\n'
        '  "NAME" VARCHAR2(100),\n'
        '  "STATUS" VARCHAR2(10) DEFAULT \'NEW\' NOT NULL\n'
        ");\n"
    )
    assert session.executed[0][0].rstrip().endswith("ORDER BY column_id")


def test_synonym_rendering() -> None:
    """A synonym is rendered from DBA_SYNONYMS, including a db link."""
    session = FakeSession({"dba_synonyms": [("HR", "EMPLOYEES", None)]})
    text = DictionaryStrategy(session).extract(_obj(ObjectType.SYNONYM, "EMP")).text
    assert text == 'CREATE OR REPLACE SYNONYM "HR"."EMP" FOR "HR"."EMPLOYEES";\n'


def test_public_synonym_rendering() -> None:
    """A PUBLIC synonym is not schema-qualified."""
    session = FakeSession({"dba_synonyms": [("HR", "EMPLOYEES", "REMOTE")]})
    obj = OracleObject(
        owner="PUBLIC", object_type=ObjectType.SYNONYM, object_name="EMP", database="TESTDB"
    )
    text = DictionaryStrategy(session).extract(obj).text
    assert text == 'CREATE OR REPLACE PUBLIC SYNONYM EMP FOR "HR"."EMPLOYEES"@REMOTE;\n'


def test_metadata_extraction_reads_the_full_clob() -> None:
    """DBMS_METADATA output is read in full and never truncated."""
    session = FakeSession({"GET_DDL": [("CREATE SEQUENCE hr.s START WITH 1;",)]})
    source = MetadataStrategy(session).extract(_obj(ObjectType.SEQUENCE, "S"))
    assert source.text == "CREATE SEQUENCE hr.s START WITH 1;\n"
    _, binds = session.executed[0]
    assert binds == {"object_type": "SEQUENCE", "name": "S", "owner": "HR"}


# --------------------------------------------------------------------------- validation


def _dispatcher(session: FakeSession) -> ExtractionDispatcher:
    return ExtractionDispatcher(session, Settings())


def test_vanished_object_is_not_a_failure() -> None:
    """A drop between discovery and extraction is skipped, not failed (§22.4)."""
    with pytest.raises(ObjectVanished):
        _dispatcher(FakeSession()).extract(_obj(ObjectType.PACKAGE))


def test_whitespace_only_source_is_rejected() -> None:
    """A blank extraction must never overwrite a good file."""
    session = FakeSession({"dba_source": [("   \n",)]})
    with pytest.raises(ExtractionError, match="whitespace only"):
        _dispatcher(session).extract(_obj(ObjectType.PACKAGE))


def test_source_without_the_expected_leading_construct_is_rejected() -> None:
    """Garbage that is not a CREATE statement fails validation."""
    obj = _obj(ObjectType.TABLE, "EMPLOYEES")
    with pytest.raises(ExtractionError, match="CREATE TABLE"):
        ExtractionDispatcher.validate(ExtractedSource(obj=obj, text="junk output\n"))


def test_valid_source_passes_the_gate() -> None:
    """A well-formed extraction is accepted."""
    session = FakeSession({"dba_source": [("PACKAGE p AS END;\n",)]})
    source = _dispatcher(session).extract(_obj(ObjectType.PACKAGE))
    assert source.text.startswith("CREATE OR REPLACE PACKAGE")


def test_extraction_error_names_the_object() -> None:
    """A failure is traceable to owner, name, and type."""
    session = FakeSession()
    session.failures["dba_source"] = RuntimeError("ORA-01031")
    with pytest.raises(ExtractionError, match=r"HR\.EMPLOYEE_PKG"):
        _dispatcher(session).extract(_obj(ObjectType.PACKAGE))
