"""Pipeline wiring: watermark resolution, concurrency, aggregation, exit codes.

The pipeline is where "state advances only after a successful push" and "a
failure in one database must not stop the others" become observable, so those
are asserted here even though extraction and the Git phase are still to come.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.config.models import AppConfig, DatabaseConfig, RetrySettings, Settings
from src.models.database import SchemaRuntime
from src.models.execution import ExecutionStatus, ExitCode, RunUnitResult
from src.state.execution_state import ExecutionStateStore
from src.state.models import ExecutionState, SchemaState
from src.workflow.pipeline import Pipeline
from tests.conftest import FakeConnectionFactory, FakeSession

MOMENT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _db(name: str) -> DatabaseConfig:
    return DatabaseConfig(
        name=name,
        git_folder=name,
        dsn_env=f"{name}_DSN",
        username_env=f"{name}_USER",
        password_env=f"{name}_PASSWORD",
    )


def _pipeline(
    tmp_path: Path,
    databases: tuple[DatabaseConfig, ...] = (),
    state: ExecutionState | None = None,
    settings: Settings | None = None,
    factory: object | None = None,
    git_client: object | None = None,
    **flags: bool,
) -> Pipeline:
    """Build a pipeline confined to ``tmp_path``.

    ``no_git`` defaults to true and the repository root is forced under
    ``tmp_path``: a pipeline test must never write to, or commit in, the
    developer's own checkout.
    """
    settings = replace(
        settings or Settings(), repository_root=str(tmp_path / "repository")
    )
    flags.setdefault("no_git", True)
    config = AppConfig(databases=databases, settings=settings, **flags)
    store = ExecutionStateStore(tmp_path / "execution_state.json")
    return Pipeline(
        config,
        state or ExecutionState(),
        store,
        execution_id="test",
        connection_factory=factory or FakeConnectionFactory(),
        git_client=git_client,
    )


# --------------------------------------------------------------------------- watermarks


def test_no_state_means_full_export(tmp_path: Path) -> None:
    """A database with no recorded run exports everything."""
    pipeline = _pipeline(tmp_path)
    runtime = pipeline._runtime_for(_db("HRDB"), "HR", pipeline.state)
    assert runtime.watermark is None
    assert runtime.is_full_export is True


def test_watermark_has_the_overlap_subtracted(tmp_path: Path) -> None:
    """The safety overlap guards against DATE truncation and clock skew."""
    state = ExecutionState().with_schema(
        "HRDB", SchemaState("HR", last_successful_run=MOMENT)
    )
    pipeline = _pipeline(tmp_path, state=state)
    runtime = pipeline._runtime_for(_db("HRDB"), "HR", state)
    assert runtime.watermark == datetime(2026, 1, 1, 11, 55, tzinfo=UTC)
    assert runtime.is_full_export is False


def test_overlap_is_configurable(tmp_path: Path) -> None:
    """A zero overlap uses the recorded watermark verbatim."""
    state = ExecutionState().with_schema(
        "HRDB", SchemaState("HR", last_successful_run=MOMENT)
    )
    settings = replace(Settings(), watermark_overlap_minutes=0)
    pipeline = _pipeline(tmp_path, state=state, settings=settings)
    runtime = pipeline._runtime_for(_db("HRDB"), "HR", state)
    assert runtime.watermark == MOMENT


def test_git_folder_is_rooted_under_the_repository(tmp_path: Path) -> None:
    """Each database writes only inside its own subtree."""
    pipeline = _pipeline(tmp_path)
    runtime = pipeline._runtime_for(_db("HRDB"), "HR", pipeline.state)
    assert runtime.git_folder == tmp_path / "repository" / "HRDB"


# --------------------------------------------------------------------------- execution


def test_no_databases_is_a_successful_no_op(tmp_path: Path) -> None:
    """An empty configuration exits 0 having done nothing."""
    result = _pipeline(tmp_path).run()
    assert result.status is ExecutionStatus.NO_CHANGES
    assert result.exit_code is ExitCode.SUCCESS


def test_every_schema_of_every_database_produces_a_run_unit(tmp_path: Path) -> None:
    """The unit of work is the schema, and order is configuration order."""
    databases = tuple(_db(name) for name in ("CDB", "ADB", "BDB"))
    factory = FakeConnectionFactory(session=_schema_only_session("HR", "PAYROLL"))
    result = _pipeline(tmp_path, databases, factory=factory).run()
    assert [(u.database, u.schema) for u in result.run_units] == [
        ("CDB", "HR"), ("CDB", "PAYROLL"),
        ("ADB", "HR"), ("ADB", "PAYROLL"),
        ("BDB", "HR"), ("BDB", "PAYROLL"),
    ]


def test_disabled_databases_are_skipped(tmp_path: Path) -> None:
    """Only enabled databases are processed."""
    databases = (_db("ON"), replace(_db("OFF"), enabled=False))
    factory = FakeConnectionFactory(session=_schema_only_session("HR"))
    result = _pipeline(tmp_path, databases, factory=factory).run()
    assert [unit.database for unit in result.run_units] == ["ON"]


def test_a_configured_schema_the_database_does_not_report_is_skipped(
    tmp_path: Path,
) -> None:
    """Safety property, not an optimization.

    Were such a schema processed, its inventory would come back empty and
    reconciliation would read that as "every object was dropped" and stage the
    deletion of every file under it.
    """
    from src.config.models import SchemaFilter

    database = replace(_db("HRDB"), schemas=SchemaFilter(include=("HR", "TYPO")))
    factory = FakeConnectionFactory(session=_schema_only_session("HR"))
    result = _pipeline(tmp_path, (database,), factory=factory).run()
    assert [unit.schema for unit in result.run_units] == ["HR"]


def test_state_is_never_written_during_a_run(tmp_path: Path) -> None:
    """State advances only after a successful push — which has not happened."""
    pipeline = _pipeline(tmp_path, (_db("HRDB"),))
    pipeline.run()
    assert pipeline.state_store.saved is False
    assert not pipeline.state_store.path.exists()


def test_dry_run_suppresses_the_git_phase(tmp_path: Path) -> None:
    """Dry run touches nothing at all."""
    pipeline = _pipeline(tmp_path, (_db("HRDB"),), dry_run=True)
    assert pipeline.run().pushed is False
    assert pipeline.state_store.saved is False


def test_pools_are_closed_after_the_extraction_phase(tmp_path: Path) -> None:
    """Connections are released even when a run unit fails."""
    factory = FakeConnectionFactory()
    _pipeline(tmp_path, (_db("HRDB"),), factory=factory).run()
    assert factory.closed is True


def test_a_database_failure_stops_the_execution(tmp_path: Path) -> None:
    """Decision D-17, reversing the old "other databases continue" rule.

    Safe only because state is per schema and written between each schema's
    push and the next schema's work: everything completed before the failure
    is already pushed and recorded, so stopping loses nothing and the next run
    resumes where this one stopped.
    """
    from src.exceptions import ConnectionError as OracleConnectionError

    factory = FakeConnectionFactory(error=OracleConnectionError("no listener"))
    result = _pipeline(tmp_path, (_db("ADB"), _db("BDB")), factory=factory).run()

    assert result.run_units == []          # BDB was never attempted
    assert result.status is ExecutionStatus.FAILED
    assert result.exit_code is ExitCode.FAILURE
    assert "no listener" in (result.stopped_by or "")


def test_discovered_objects_are_counted(tmp_path: Path) -> None:
    """Discovery and extraction counts reach the run-unit summary."""
    session = FakeSession({
        "dba_objects": [("HR", "PACKAGE", "P1", "VALID", None, None)],
        "dba_source": [("PACKAGE p1 AS END;\n",)],
    })
    factory = FakeConnectionFactory(session=session)
    result = _pipeline(tmp_path, (_db("HRDB"),), factory=factory).run()
    unit = result.run_units[0]
    assert unit.objects_discovered == 1
    assert unit.objects_exported == 1
    assert unit.objects_failed == 0


def test_vanished_object_is_skipped_not_failed(tmp_path: Path) -> None:
    """An object dropped mid-run is a warning, not a failure (§22.4)."""
    session = FakeSession({"dba_objects": [("HR", "PACKAGE", "GONE", "VALID", None, None)]})
    factory = FakeConnectionFactory(session=session)
    unit = _pipeline(tmp_path, (_db("HRDB"),), factory=factory).run().run_units[0]
    assert unit.objects_skipped == 1
    assert unit.objects_failed == 0
    assert unit.objects_exported == 0


def test_normalization_failure_is_contained_to_its_object() -> None:
    """A bad object is FAILED_NORMALIZATION; the run unit keeps going."""
    from src.exceptions import NormalizationError
    from src.models.oracle_object import ObjectStatus
    from src.workflow.run_unit import RunUnit

    class ExplodingNormalizer:
        def normalize(self, source: str, object_type=None) -> str:
            raise NormalizationError("boom")

    session = FakeSession({
        "dba_objects": [
            ("HR", "PACKAGE", "P1", "VALID", None, None),
            ("HR", "PACKAGE", "P2", "VALID", None, None),
        ],
        "dba_source": [("PACKAGE p AS END;\n",)],
    })
    runtime = SchemaRuntime(
        name="HRDB", schema="HR", git_folder=Path("HRDB"), watermark=None
    )
    result = RunUnit(
        _db("HRDB"),
        runtime,
        Settings(),
        session,
        normalizer=ExplodingNormalizer(),
    ).run()

    assert result.objects_discovered == 2
    assert result.objects_failed == 2  # the second object was still attempted
    assert {r.status for r in result.object_results} == {ObjectStatus.FAILED_NORMALIZATION}


def test_normalized_source_reaches_the_normalizer() -> None:
    """The run unit hands extracted text to the normalizer with its type."""
    from src.models.oracle_object import ObjectType
    from src.workflow.run_unit import RunUnit

    seen: list[tuple[str, ObjectType | None]] = []

    class RecordingNormalizer:
        def normalize(self, source: str, object_type=None) -> str:
            seen.append((source, object_type))
            return source

    session = FakeSession({
        "dba_objects": [("HR", "PACKAGE", "P1", "VALID", None, None)],
        "dba_source": [("PACKAGE p1 AS END;\n",)],
    })
    runtime = SchemaRuntime(
        name="HRDB", schema="HR", git_folder=Path("HRDB"), watermark=None
    )
    RunUnit(
        _db("HRDB"), runtime, Settings(),
        session, normalizer=RecordingNormalizer(),
    ).run()

    assert len(seen) == 1
    assert seen[0][0].startswith("CREATE OR REPLACE PACKAGE")
    assert seen[0][1] is ObjectType.PACKAGE


def _run_unit(tmp_path: Path, session: FakeSession, database=None, **settings_kw):
    """Build a run unit writing into ``tmp_path``, with a fake session."""
    from src.workflow.run_unit import RunUnit

    database = database or _db("HRDB")
    runtime = SchemaRuntime(
        name="HRDB", schema="HR", git_folder=tmp_path / "HRDB", watermark=None
    )
    return RunUnit(
        database,
        runtime,
        replace(Settings(), **settings_kw) if settings_kw else Settings(),
        session,
    )


def _package_session(*names: str, schemas: tuple[str, ...] = ("HR",)) -> FakeSession:
    return FakeSession({
        # The schema-list query (SELECT DISTINCT owner) is what the pipeline
        # uses to decide which schemas to process at all; without it a
        # pipeline-level test runs nothing (decision D-15).
        "DISTINCT owner": [(name,) for name in schemas],
        "dba_objects": [("HR", "PACKAGE", name, "VALID", None, None) for name in names],
        "dba_source": [("PACKAGE p AS\n",), ("END;\n",)],
    })


def _schema_only_session(*schemas: str) -> FakeSession:
    """A session that reports schemas but no objects in them."""
    return FakeSession({"DISTINCT owner": [(name,) for name in schemas]})


# --------------------------------------------------------------------------- writing


def test_objects_are_written_to_their_mapped_paths(tmp_path: Path) -> None:
    """The full chain: discover, extract, normalize, write."""
    result = _run_unit(tmp_path, _package_session("P1")).run()
    target = tmp_path / "HRDB" / "HR" / "packages" / "P1.sql"

    assert result.written_paths == [target]
    assert target.read_bytes() == b"CREATE OR REPLACE PACKAGE p AS\nEND;\n/\n"


def test_written_files_use_lf_and_one_trailing_newline(tmp_path: Path) -> None:
    """The on-disk conventions hold end to end, not just in the writer."""
    _run_unit(tmp_path, _package_session("P1")).run()
    raw = (tmp_path / "HRDB" / "HR" / "packages" / "P1.sql").read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")


def test_a_second_identical_run_produces_byte_identical_files(tmp_path: Path) -> None:
    """The whole no-empty-commit design rests on this being true."""
    _run_unit(tmp_path, _package_session("P1")).run()
    target = tmp_path / "HRDB" / "HR" / "packages" / "P1.sql"
    first = target.read_bytes()

    _run_unit(tmp_path, _package_session("P1")).run()
    assert target.read_bytes() == first


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """Dry run is fully live up to the writer, which touches nothing."""
    from src.repository.writer import NullWriter
    from src.workflow.run_unit import RunUnit

    writer = NullWriter()
    runtime = SchemaRuntime(
        name="HRDB", schema="HR", git_folder=tmp_path / "HRDB", watermark=None
    )
    result = RunUnit(
        _db("HRDB"), runtime, Settings(),
        _package_session("P1"), writer=writer,
    ).run()

    assert result.objects_exported == 1
    assert writer.would_write == [tmp_path / "HRDB" / "HR" / "packages" / "P1.sql"]
    assert not (tmp_path / "HRDB").exists()


def test_unmappable_name_fails_only_its_own_object(tmp_path: Path) -> None:
    """Decision D-5, contained: the other objects are still exported."""
    from src.models.oracle_object import ObjectStatus

    session = FakeSession({
        "dba_objects": [
            ("HR", "PACKAGE", "BAD/NAME", "VALID", None, None),
            ("HR", "PACKAGE", "GOOD", "VALID", None, None),
        ],
        "dba_source": [("PACKAGE p AS\n",), ("END;\n",)],
    })
    result = _run_unit(tmp_path, session).run()

    assert result.objects_failed == 1
    assert result.objects_exported == 1
    statuses = {r.obj.object_name: r.status for r in result.object_results}
    assert statuses["BAD/NAME"] is ObjectStatus.FAILED_COMPARISON
    assert statuses["GOOD"] is ObjectStatus.SUCCESS


# --------------------------------------------------------------------------- reconciliation


def test_dropped_object_file_is_reported_for_deletion(tmp_path: Path) -> None:
    """LAST_DDL_TIME cannot see a drop; reconciliation can (§22.4)."""
    stale = tmp_path / "HRDB" / "HR" / "packages" / "DROPPED.sql"
    stale.parent.mkdir(parents=True)
    stale.write_text("old\n", encoding="utf-8")
    for extra in range(20):  # keep the deletion under the 10% threshold
        (stale.parent / f"KEEP{extra}.sql").write_text("x\n", encoding="utf-8")

    session = FakeSession({
        "dba_objects": [
            ("HR", "PACKAGE", f"KEEP{i}", "VALID", None, None) for i in range(20)
        ],
        "dba_source": [("PACKAGE p AS\n",), ("END;\n",)],
    })
    result = _run_unit(tmp_path, session).run()

    assert result.deleted_paths == [stale]
    assert result.reconciliation_blocked is False


def test_mass_deletion_is_blocked_and_nothing_is_deleted(tmp_path: Path) -> None:
    """Decision D-6: a wholesale mismatch is a human decision, not automatic."""
    folder = tmp_path / "HRDB" / "HR" / "packages"
    folder.mkdir(parents=True)
    for index in range(10):
        (folder / f"OLD{index}.sql").write_text("x\n", encoding="utf-8")

    result = _run_unit(tmp_path, FakeSession({"dba_objects": []})).run()

    assert result.deleted_paths == []
    assert result.reconciliation_blocked is True


def test_reconciliation_is_skipped_when_the_inventory_query_fails(tmp_path: Path) -> None:
    """A transient failure must never read as "everything was dropped"."""
    folder = tmp_path / "HRDB" / "HR" / "packages"
    folder.mkdir(parents=True)
    (folder / "KEEP.sql").write_text("x\n", encoding="utf-8")

    session = _package_session("KEEP")
    calls: list[int] = []
    original = session.fetch_all

    def fail_the_inventory_query(statement: str, binds=None):
        # Identify the discovery/inventory query by its column list, so the
        # cheap pre-flight probe against dba_objects is not miscounted. The
        # first such query is discovery; the second is the inventory.
        if "owner, object_type" in statement:
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("ORA-03113")
        return original(statement, binds)

    session.fetch_all = fail_the_inventory_query  # type: ignore[method-assign]
    result = _run_unit(tmp_path, session).run()

    assert result.reconciliation_skipped is True
    assert result.deleted_paths == []
    assert result.status is not ExecutionStatus.FAILED  # extraction still counted


def test_reconciliation_ignores_out_of_scope_schemas(tmp_path: Path) -> None:
    """Removing a schema from the include filter must not delete its history."""
    from src.config.models import SchemaFilter

    payroll = tmp_path / "HRDB" / "PAYROLL" / "packages" / "LEGACY.sql"
    payroll.parent.mkdir(parents=True)
    payroll.write_text("x\n", encoding="utf-8")

    database = replace(_db("HRDB"), schemas=SchemaFilter(include=("HR",)))
    result = _run_unit(tmp_path, FakeSession({"dba_objects": []}), database=database).run()

    assert result.deleted_paths == []
    assert payroll.exists()


def test_missing_grants_fail_the_database_by_name(tmp_path: Path) -> None:
    """A missing grant is reported up front, not part-way through (§21.2).

    Checked once per database, before its schema list is even resolved, so a
    missing grant cannot be discovered halfway through the third schema.
    """
    session = _package_session("P1")
    session.failures["dba_views"] = RuntimeError("ORA-00942: table or view does not exist")
    factory = FakeConnectionFactory(session=session)

    result = _pipeline(tmp_path, (_db("HRDB"),), factory=factory).run()

    assert result.status is ExecutionStatus.FAILED
    assert result.run_units == []                       # nothing was attempted
    assert "dba_views" in (result.stopped_by or "")     # named, not guessed at


def test_preflight_passes_when_every_view_is_readable(tmp_path: Path) -> None:
    """The happy path is not blocked by the check."""
    factory = FakeConnectionFactory(session=_package_session("P1"))
    result = _pipeline(tmp_path, (_db("HRDB"),), factory=factory).run()
    assert result.run_units[0].objects_exported == 1


def test_wall_clock_timeout_stops_the_run_unit(tmp_path: Path) -> None:
    """A slow database must not consume the whole CI job timeout (§15).

    Raised now rather than reported as a status: under D-17 a schema-level
    failure stops the execution, and the caller turns it into the summary.
    """
    from src.config.models import RetrySettings
    from src.exceptions import DatabaseError

    session = _package_session(*[f"P{i}" for i in range(5)])
    unit = _run_unit(tmp_path, session, retry=replace(RetrySettings(), run_unit_timeout_seconds=0))

    with pytest.raises(DatabaseError, match="wall-clock timeout"):
        unit.run()


def test_generous_timeout_does_not_interfere(tmp_path: Path) -> None:
    """The guard must not fire during a normal run."""
    from src.config.models import RetrySettings

    unit = _run_unit(
        tmp_path, _package_session("P1"),
        retry=replace(RetrySettings(), run_unit_timeout_seconds=3600),
    )
    assert unit.run().objects_exported == 1


def test_candidate_watermark_comes_from_the_database_clock(tmp_path: Path) -> None:
    """Never the runner clock — skew would cause missed changes (§4.2)."""
    factory = FakeConnectionFactory(session=_schema_only_session("HR"))
    unit = _pipeline(tmp_path, (_db("HRDB"),), factory=factory).run().run_units[0]
    assert unit.candidate_watermark == datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- aggregation


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone on `main` wired to a bare remote, both under tmp_path."""
    import subprocess

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()

    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")

    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "Seed")
    git(repo, "config", "user.email", "seed@example.invalid")
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "seed")
    git(repo, "push", "-u", "origin", "main")
    return repo, remote


class _NoToken:
    def resolve(self, name: str) -> str:
        raise RuntimeError(f"{name} unset")


def _git_pipeline(repo: Path, session: FakeSession, **flags):
    """A pipeline committing into a real temporary repository."""
    from src.git.git_client import GitClient

    settings = replace(
        Settings(),
        repository_root=str(repo / "repository"),
        retry=replace(RetrySettings(), push_attempts=1),
    )
    config = AppConfig(
        databases=(_db("HRDB"),), settings=settings, **flags
    )
    store = ExecutionStateStore(repo / "state" / "execution_state.json")
    return Pipeline(
        config,
        ExecutionState(),
        store,
        execution_id="test",
        connection_factory=FakeConnectionFactory(session=session),
        git_client=GitClient(repo),
        secret_provider=_NoToken(),
    )


def test_full_git_phase_commits_pushes_then_advances_state(tmp_path: Path) -> None:
    """The complete D-3 sequence, against a real repository and remote.

    Commit and push the DDL first; only then write state, amend it into the
    same commit, and force-push under a lease — one commit per run.
    """
    import subprocess

    repo, remote = _repo_with_remote(tmp_path)
    pipeline = _git_pipeline(repo, _package_session("EMP_PKG"))
    result = pipeline.run()

    assert result.pushed is True
    assert result.commit_sha

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()

    # Exactly one commit was added on top of the seed (amend, not a second commit).
    assert len(git(remote, "log", "--pretty=%H", "refs/heads/main").splitlines()) == 2
    files = git(remote, "show", "--name-only", "--pretty=", "refs/heads/main").split()
    assert "state/execution_state.json" in files
    assert any(name.endswith("EMP_PKG.sql") for name in files)

    # The published message must still list the objects. Regression: it was
    # rebuilt during the amend, when only the state file was staged, and the
    # amend replaces the message — so the list vanished from the real commit.
    message = git(remote, "log", "-1", "--pretty=%B", "refs/heads/main")
    assert "HR.EMP_PKG (PACKAGE) [ADDED]" in message
    assert "Number of objects  : 1" in message

    # State advanced to the database clock only after the push succeeded.
    assert pipeline.state_store.saved is True
    assert pipeline.state.watermark_for("HRDB", "HR") == datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def test_unchanged_second_run_creates_no_commit(tmp_path: Path) -> None:
    """Recompilation-only runs are silent: no commit, no push, no state (§22.3b)."""
    import subprocess

    repo, remote = _repo_with_remote(tmp_path)
    _git_pipeline(repo, _package_session("EMP_PKG")).run()
    first = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"], cwd=remote,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    second = _git_pipeline(repo, _package_session("EMP_PKG"))
    result = second.run()

    assert result.status is ExecutionStatus.NO_CHANGES
    assert result.pushed is False
    assert second.state_store.saved is False
    after = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"], cwd=remote,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert after == first


def test_state_is_not_advanced_when_the_push_fails(tmp_path: Path) -> None:
    """The single most important rule: no watermark past unpushed work."""
    from src.exceptions import PushError

    repo, _ = _repo_with_remote(tmp_path)
    pipeline = _git_pipeline(repo, _package_session("EMP_PKG"))
    pipeline.git_client.run(["remote", "set-url", "origin", "https://127.0.0.1:1/nope.git"])

    result = pipeline.run()

    assert result.status is ExecutionStatus.FAILED
    assert result.pushed is False
    assert pipeline.state_store.saved is False
    assert not pipeline.state_store.path.exists()
    assert issubclass(PushError, Exception)


def test_a_commit_stranded_by_an_earlier_failed_push_is_recovered(tmp_path: Path) -> None:
    """Regression: found live against a real database.

    A run committed locally and then failed to push. The next run re-extracted
    identical content, so nothing new was staged — and the old code concluded
    "nothing to do" without ever noticing HEAD was still sitting unpushed. The
    commit would have been stranded forever, since architecture §22.7's
    recovery story assumes the next run always produces a fresh diff, which is
    false whenever the content is unchanged.

    Checked once per execution rather than once per schema: a stranded commit
    is a property of the repository, and the check costs a network round trip.
    """
    import subprocess

    def sha(cwd: Path) -> str:
        return subprocess.run(
            ["git", "rev-parse", "refs/heads/main"], cwd=cwd,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    repo, remote = _repo_with_remote(tmp_path)

    # First run writes the files but never touches git, then we commit them by
    # hand and deliberately do not push — exactly the state a failed push
    # leaves behind, since the commit already exists by the time push runs.
    _git_pipeline(repo, _package_session("EMP_PKG"), no_git=True).run()
    written = list((repo / "repository").rglob("*.sql"))
    assert written, "the first run should have written the package"

    stranded = _git_pipeline(repo, _package_session("EMP_PKG"))
    stranded.git_client.configure_identity("Bot", "bot@example.invalid")
    stranded.git_client.add(written)
    stranded.git_client.commit("stranded\n")
    stranded_sha = stranded.git_client.rev_parse("HEAD")

    remote_before = sha(remote)
    assert stranded_sha != remote_before  # confirms the stranding actually happened

    # The next run re-extracts identical content: nothing new to stage.
    recovering = _git_pipeline(repo, _package_session("EMP_PKG"))
    result = recovering.run()

    assert sha(remote) == stranded_sha  # the stranded commit finally reached the remote
    assert result.pushed is True


@pytest.mark.parametrize(
    ("statuses", "expected", "exit_code"),
    [
        ([], ExecutionStatus.NO_CHANGES, ExitCode.SUCCESS),
        ([ExecutionStatus.NO_CHANGES], ExecutionStatus.NO_CHANGES, ExitCode.SUCCESS),
        ([ExecutionStatus.SUCCESS], ExecutionStatus.SUCCESS, ExitCode.SUCCESS),
        (
            [ExecutionStatus.SUCCESS, ExecutionStatus.NO_CHANGES],
            ExecutionStatus.SUCCESS,
            ExitCode.SUCCESS,
        ),
        (
            [ExecutionStatus.NO_CHANGES, ExecutionStatus.NO_CHANGES],
            ExecutionStatus.NO_CHANGES,
            ExitCode.SUCCESS,
        ),
    ],
)
def test_status_aggregation(
    statuses: list[ExecutionStatus], expected: ExecutionStatus, exit_code: ExitCode
) -> None:
    """Aggregation only ever sees schemas that completed.

    There is no FAILED or PARTIAL_FAILURE case to aggregate any more: under
    D-17 a schema-level failure stops the execution, and the status is set on
    the way out rather than derived from the units.
    """
    from src.models.execution import ExecutionResult

    result = ExecutionResult(execution_id="x")
    result.run_units = [
        RunUnitResult(database="HRDB", schema=f"S{i}", status=status)
        for i, status in enumerate(statuses)
    ]
    result.status = Pipeline._aggregate(result)
    assert result.status is expected
    assert result.exit_code is exit_code


def test_a_stopped_execution_is_failed_regardless_of_what_completed() -> None:
    """Schemas that succeeded before the failure do not make the run green."""
    from src.models.execution import ExecutionResult

    result = ExecutionResult(execution_id="x")
    result.run_units = [
        RunUnitResult(database="HRDB", schema="HR", status=ExecutionStatus.SUCCESS, pushed=True)
    ]
    result.status = ExecutionStatus.FAILED
    result.stopped_by = "no listener"

    assert result.exit_code is ExitCode.FAILURE


def test_a_stranded_commit_alone_makes_the_run_a_success() -> None:
    """Pushing a previous run's commit is a real change, even if no schema moved."""
    from src.models.execution import ExecutionResult

    result = ExecutionResult(execution_id="x", pushed=True, commit_sha="abc")
    result.run_units = [
        RunUnitResult(database="HRDB", schema="HR", status=ExecutionStatus.NO_CHANGES)
    ]
    assert Pipeline._aggregate(result) is ExecutionStatus.SUCCESS
