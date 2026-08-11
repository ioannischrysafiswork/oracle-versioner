"""Shared fixtures.

The whole unit suite runs with **no Oracle, no network, and no GitLab**
(docs/architecture.md §17.1). Anything needing a real database is marked
``@pytest.mark.oracle`` and skipped by default.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.config.models import AppConfig, DatabaseConfig, Settings
from src.models.oracle_object import ObjectType, OracleObject

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Used by the redaction test. Must never appear in any emitted log record.
SENTINEL_PASSWORD = "s3ntinel-must-never-be-logged"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip Oracle- and network-dependent tests unless explicitly requested.

    Args:
        config: The pytest configuration.
        items: The collected test items, modified in place.
    """
    if config.getoption("-m"):
        return
    skip_oracle = pytest.mark.skip(reason="needs a live Oracle instance; select with -m oracle")
    skip_integration = pytest.mark.skip(reason="needs network; select with -m integration")
    for item in items:
        if "oracle" in item.keywords:
            item.add_marker(skip_oracle)
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


@pytest.fixture
def project_root() -> Path:
    """The repository root."""
    return PROJECT_ROOT


@pytest.fixture(autouse=True)
def never_touch_the_project_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to run any Git command against this project's own repository.

    :class:`~src.workflow.pipeline.Pipeline` defaults its Git client to the
    current directory, which is correct in production — the app runs inside the
    CI checkout — but means a test that forgets to inject one will commit to
    the developer's working repository. That happened once during development:
    a pipeline test created a real commit on ``main``, wrote a bot identity
    into ``.git/config``, and attempted a push. This fixture makes that
    impossible rather than merely unlikely.
    """
    from src.git.git_client import GitClient

    original = GitClient.run

    def guarded(self: GitClient, args, **kwargs):
        work_tree = Path(self.work_tree).resolve()
        if work_tree == PROJECT_ROOT or PROJECT_ROOT in work_tree.parents:
            raise AssertionError(
                f"test tried to run 'git {' '.join(args)}' against the project "
                f"repository at {work_tree}. Inject a GitClient bound to tmp_path."
            )
        return original(self, args, **kwargs)

    monkeypatch.setattr(GitClient, "run", guarded)


@pytest.fixture
def database_config() -> DatabaseConfig:
    """A minimal database configuration carrying secret names only."""
    return DatabaseConfig(
        name="TESTDB",
        git_folder="TESTDB",
        dsn_env="TESTDB_DSN",
        username_env="TESTDB_USER",
        password_env="TESTDB_PASSWORD",
    )


@pytest.fixture
def app_config(database_config: DatabaseConfig) -> AppConfig:
    """An application configuration with one enabled database."""
    return AppConfig(databases=(database_config,), settings=Settings())


class FakeSession:
    """A scripted stand-in for an Oracle session.

    Rows are keyed by a distinguishing fragment of the statement, so a test
    says what ``DBA_SOURCE`` returns without reproducing the whole query.
    Every executed statement is recorded for assertions about binds and guards.
    """

    def __init__(
        self,
        rows: dict[str, list[tuple]] | None = None,
        server_major_version: int = 19,
    ) -> None:
        self._rows = rows or {}
        self._version = server_major_version
        #: The server's UTC offset. Deliberately not UTC by default in some
        #: tests: a database in another zone is where the watermark bug lived.
        self.server_timezone = UTC
        self.executed: list[tuple[str, dict]] = []
        self.failures: dict[str, Exception] = {}

    @property
    def server_major_version(self) -> int:
        return self._version

    def fetch_all(self, statement: str, binds: dict | None = None) -> list[tuple]:
        from src.oracle.connection import assert_read_only

        assert_read_only(statement)
        self.executed.append((statement, dict(binds or {})))
        for fragment, exc in self.failures.items():
            if fragment in statement:
                raise exc
        for fragment, rows in self._rows.items():
            if fragment in statement:
                return list(rows)
        return []

    def fetch_value(self, statement: str, binds: dict | None = None):
        rows = self.fetch_all(statement, binds)
        return rows[0][0] if rows else None

    def database_time(self):
        """Server clock, timezone-aware, as OracleSession returns it."""
        return datetime(2026, 8, 1, 9, 0, tzinfo=self.server_timezone)


class FakeConnectionFactory:
    """Yields a :class:`FakeSession`, or raises to simulate an outage."""

    def __init__(self, session: object | None = None, error: Exception | None = None) -> None:
        self.fake_session = session or FakeSession()
        self.error = error
        self.closed = False

    @contextmanager
    def session(self, database):
        if self.error is not None:
            raise self.error
        yield self.fake_session

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_session() -> FakeSession:
    """A session returning nothing unless a test scripts rows."""
    return FakeSession()


@pytest.fixture
def oracle_object() -> OracleObject:
    """A representative discovered object."""
    return OracleObject(
        owner="HR",
        object_type=ObjectType.PACKAGE_BODY,
        object_name="EMPLOYEE_PKG",
        database="TESTDB",
        last_ddl_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )
