"""Integration tests against a real Oracle instance (docs/architecture.md §17.2).

Everything here is marked ``@pytest.mark.oracle`` and skipped by default, so
the offline suite stays runnable on any laptop with no database. These cover
precisely what the fakes cannot: that the driver connects in thin mode, that
the session-initialization statements are accepted by a real server, that the
discovery query is valid SQL against a real ``DBA_OBJECTS``, and that
``DBMS_METADATA`` returns storage-free DDL with the configured transforms.

Run them with a disposable instance::

    export ORACLE_TEST_DSN=localhost:1521/FREEPDB1
    export ORACLE_TEST_USER=ddl_reader
    export ORACLE_TEST_PASSWORD=...
    python -m pytest -m oracle -v

**Every test here is read-only.** Nothing creates, alters, or drops anything —
including the privilege check, which reads ``SESSION_PRIVS`` rather than
attempting a write to see whether it is refused. Attempting one would risk
succeeding.

The tests are written to tolerate an instance that simply has no objects of a
given type: they skip rather than fail, so they are useful against a bare
container and against a populated copy of production alike.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from src.config.models import DatabaseConfig, Settings
from src.models.database import SchemaRuntime
from src.models.oracle_object import ObjectType
from src.normalizer.ddl_normalizer import DdlNormalizer
from src.oracle import sql
from src.oracle.connection import ConnectionFactory, preflight
from src.oracle.metadata import ExtractionDispatcher, ObjectVanished
from src.oracle.object_discovery import ObjectDiscovery

pytestmark = pytest.mark.oracle

DSN = os.environ.get("ORACLE_TEST_DSN")
USER = os.environ.get("ORACLE_TEST_USER")
PASSWORD = os.environ.get("ORACLE_TEST_PASSWORD")

requires_instance = pytest.mark.skipif(
    not (DSN and USER and PASSWORD),
    reason="set ORACLE_TEST_DSN, ORACLE_TEST_USER and ORACLE_TEST_PASSWORD",
)

#: Privileges that would let the agent modify a source database. Read from
#: SESSION_PRIVS rather than probed by attempting a write.
FORBIDDEN_PRIVILEGES = frozenset(
    {
        "CREATE TABLE", "CREATE ANY TABLE", "ALTER ANY TABLE", "DROP ANY TABLE",
        "INSERT ANY TABLE", "UPDATE ANY TABLE", "DELETE ANY TABLE",
        "CREATE ANY PROCEDURE", "ALTER ANY PROCEDURE", "DROP ANY PROCEDURE",
        "CREATE ANY INDEX", "DROP ANY INDEX", "GRANT ANY PRIVILEGE",
        "GRANT ANY ROLE", "CREATE ANY VIEW", "DROP ANY VIEW",
    }
)


class _Provider:
    """Resolves the three test credentials from the environment."""

    def resolve(self, name: str) -> str:
        value = {"DSN": DSN, "USER": USER, "PASSWORD": PASSWORD}[name]
        assert value is not None
        return value


@pytest.fixture(scope="module")
def database() -> DatabaseConfig:
    """The instance under test, described as a normal database configuration."""
    return DatabaseConfig(
        name="ITDB", git_folder="ITDB",
        dsn_env="DSN", username_env="USER", password_env="PASSWORD",
    )


@pytest.fixture(scope="module")
def settings() -> Settings:
    """Settings with a short connect budget, so a bad DSN fails fast."""
    return replace(Settings(), retry=replace(Settings().retry, connect_attempts=1))


@pytest.fixture(scope="module")
def factory(settings: Settings):
    """A real connection factory, closed at the end of the module."""
    made = ConnectionFactory(settings, _Provider())
    yield made
    made.close()


@pytest.fixture
def session(factory: ConnectionFactory, database: DatabaseConfig):
    """An initialized read-only session against the live instance."""
    with factory.session(database) as live:
        yield live


# --------------------------------------------------------------------------- connection


@requires_instance
def test_connects_in_thin_mode_and_initializes_the_session(session) -> None:
    """Pooling plus NLS and DBMS_METADATA transforms, accepted by a real server.

    Session initialization issues real ``ALTER SESSION`` and
    ``DBMS_METADATA.SET_TRANSFORM_PARAM`` calls; a fake cannot tell us the
    server accepts them.
    """
    assert session.server_major_version >= 11


@requires_instance
def test_database_clock_is_timezone_aware(session) -> None:
    """The watermark comes from this value, and must not be naive (§4.2)."""
    now = session.database_time()
    assert now.tzinfo is not None


@requires_instance
def test_nls_settings_took_effect(session) -> None:
    """Determinism depends on these, so confirm the server really applied them."""
    rows = session.fetch_all(
        "SELECT parameter, value FROM nls_session_parameters "
        "WHERE parameter IN ('NLS_SORT', 'NLS_COMP')"
    )
    applied = dict(rows)
    assert applied.get("NLS_SORT") == "BINARY"
    assert applied.get("NLS_COMP") == "BINARY"


# --------------------------------------------------------------------------- privileges


@requires_instance
def test_preflight_reports_no_missing_grants(session) -> None:
    """The account can read every dictionary view the agent needs (§21.2)."""
    assert preflight(session) == []


@requires_instance
def test_account_holds_no_privilege_that_could_modify_the_database(session) -> None:
    """The read-only guarantee, verified at its real source: the grants.

    Deliberately *not* tested by attempting a write — if the account did have
    the privilege, the probe itself would modify the database.
    """
    granted = {row[0] for row in session.fetch_all("SELECT privilege FROM session_privs")}
    assert not (granted & FORBIDDEN_PRIVILEGES), sorted(granted & FORBIDDEN_PRIVILEGES)


# --------------------------------------------------------------------------- discovery


@requires_instance
def test_discovery_query_is_valid_sql_against_a_real_dictionary(
    session, database: DatabaseConfig, settings: Settings
) -> None:
    """Every predicate, bind, and the ORACLE_MAINTAINED version gate, for real.

    The unit tests assert the query's *text*; only a real server can confirm
    the columns exist and the binds are accepted.
    """
    discovery = ObjectDiscovery(session, database, settings)
    owner = database.schemas.include[0] if database.schemas.include else "SYS"
    runtime = SchemaRuntime(
        name="ITDB", schema=owner, git_folder=None, watermark=None  # type: ignore[arg-type]
    )
    objects = discovery.discover_changed(runtime)
    assert isinstance(objects, list)


@requires_instance
def test_inventory_query_is_valid_sql(
    session, database: DatabaseConfig, settings: Settings
) -> None:
    """Reconciliation's query must also survive contact with a real dictionary."""
    assert isinstance(ObjectDiscovery(session, database, settings).full_inventory(), list)


@requires_instance
def test_discovery_excludes_oracle_supplied_schemas(
    session, database: DatabaseConfig, settings: Settings
) -> None:
    """SYS and friends must never appear, whatever the server version."""
    inventory = ObjectDiscovery(session, database, settings).full_inventory()
    owners = {obj.owner for obj in inventory}
    assert not (owners & set(sql.INTERNAL_SCHEMAS))


@requires_instance
def test_discovery_is_ordered_and_deduplicated(
    session, database: DatabaseConfig, settings: Settings
) -> None:
    """Ordering makes the commit body deterministic (§4.1, §5.6)."""
    inventory = ObjectDiscovery(session, database, settings).full_inventory()
    keys = [(o.owner, str(o.object_type), o.object_name) for o in inventory]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------- extraction


def _first_of(inventory, object_type: ObjectType):
    """Return one object of the given type, or skip if the instance has none."""
    for obj in inventory:
        if obj.object_type is object_type:
            return obj
    pytest.skip(f"instance has no {object_type} in scope")


@requires_instance
@pytest.mark.parametrize(
    "object_type",
    [
        ObjectType.PACKAGE, ObjectType.PACKAGE_BODY, ObjectType.PROCEDURE,
        ObjectType.FUNCTION, ObjectType.TRIGGER, ObjectType.TYPE,
        ObjectType.VIEW, ObjectType.TABLE, ObjectType.SEQUENCE,
        ObjectType.SYNONYM, ObjectType.MATERIALIZED_VIEW,
    ],
)
def test_each_strategy_extracts_and_validates(
    session, database: DatabaseConfig, settings: Settings, object_type: ObjectType
) -> None:
    """Every extraction strategy, against a real object of its type.

    This is the test that would have caught VIEW being mapped to ``DBA_SOURCE``:
    the query is valid SQL, returns nothing, and every view looks dropped.
    """
    inventory = ObjectDiscovery(session, database, settings).full_inventory()
    obj = _first_of(inventory, object_type)

    try:
        source = ExtractionDispatcher(session, settings).extract(obj)
    except ObjectVanished:
        pytest.skip(f"{obj.owner}.{obj.object_name} disappeared mid-test")

    assert source.text.strip(), f"{object_type} extracted empty for {obj.owner}.{obj.object_name}"


@requires_instance
def test_dbms_metadata_output_carries_no_storage_clauses(
    session, database: DatabaseConfig, settings: Settings
) -> None:
    """The transform parameters must suppress storage noise *at the source*.

    If this fails, the textual remover is doing work the database should have
    done, and the session transforms are not being applied.
    """
    inventory = ObjectDiscovery(session, database, settings).full_inventory()
    obj = _first_of(inventory, ObjectType.SEQUENCE)
    text = ExtractionDispatcher(session, settings).extract(obj).text.upper()

    for clause in ("SEGMENT_ATTRIBUTES", "TABLESPACE", "STORAGE("):
        assert clause not in text


@requires_instance
def test_source_extraction_preserves_line_count(
    session, database: DatabaseConfig, settings: Settings
) -> None:
    """No truncation and no reordering on a real, possibly large, body."""
    inventory = ObjectDiscovery(session, database, settings).full_inventory()
    obj = _first_of(inventory, ObjectType.PACKAGE_BODY)

    expected = session.fetch_all(
        "SELECT COUNT(*) FROM dba_source WHERE owner = :owner AND name = :name "
        "AND type = :object_type",
        {"owner": obj.owner, "name": obj.object_name, "object_type": str(obj.object_type)},
    )[0][0]
    text = ExtractionDispatcher(session, settings).extract(obj).text
    # The extractor prepends CREATE OR REPLACE to line 1, so the line count is
    # unchanged; a trailing newline may add one.
    assert abs(len(text.splitlines()) - expected) <= 1


# --------------------------------------------------------------------------- round trip


@requires_instance
def test_extracted_source_normalizes_idempotently(
    session, database: DatabaseConfig, settings: Settings
) -> None:
    """The property that keeps the repository stable, on real production DDL.

    The offline corpus is synthetic; real packages are where the awkward
    constructs live.
    """
    inventory = ObjectDiscovery(session, database, settings).full_inventory()
    dispatcher = ExtractionDispatcher(session, settings)
    normalizer = DdlNormalizer(settings.normalization)

    checked = 0
    for obj in inventory[:25]:
        try:
            source = dispatcher.extract(obj)
        except Exception:  # noqa: S112 - a single bad object is not this test's subject
            continue
        once = normalizer.normalize(source.text, obj.object_type)
        assert normalizer.normalize(once, obj.object_type) == once, f"{obj.owner}.{obj.object_name}"
        checked += 1

    if not checked:
        pytest.skip("no extractable objects in scope")
