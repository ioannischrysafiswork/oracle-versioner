"""Git phase, against real temporary repositories.

No mocks of ``git`` itself: each test builds a real repository with a real bare
"remote" on the filesystem, so staging, the empty-commit gate, rebase-and-retry
on a genuine non-fast-forward, and ``--force-with-lease`` are all exercised
against the actual tool.

The rules being defended are the ones that cost the most when broken: only the
paths this execution touched are ever staged, an unchanged run creates no
commit, and state is never written before a push succeeds.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.config.models import GitSettings, RetrySettings
from src.exceptions import GitError, PushError
from src.git.commit import SKIP_CI_MARKER, CommitBuilder
from src.git.git_client import GitClient, redact_url
from src.git.push import Pusher, is_retryable_push_failure
from src.models.execution import RunUnitResult
from src.repository.file_mapper import FileMapper
from src.utils.timers import utc_now


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare repository standing in for GitLab."""
    path = tmp_path / "remote.git"
    path.mkdir()
    _git(path, "init", "--bare", "--initial-branch=main")
    return path


@pytest.fixture
def repo(tmp_path: Path, remote: Path) -> Path:
    """A working clone with one commit, on `main`, wired to the remote."""
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "remote", "add", "origin", str(remote))
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "seed")
    _git(path, "push", "-u", "origin", "main")
    return path


@pytest.fixture
def client(repo: Path) -> GitClient:
    """A client bound to the working clone."""
    return GitClient(repo)


def _write(repo: Path, relative: str, content: str = "x\n") -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _settings(**kwargs) -> GitSettings:
    from dataclasses import replace

    return replace(GitSettings(), branch="main", **kwargs)


# --------------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://oauth2:secrettoken@gitlab.com/x.git", "https://***@gitlab.com/x.git"),
        ("http://user:pw@host/y", "http://***@host/y"),
        ("fatal: unable to access 'https://a:b@h/r.git/'", "fatal: unable to access 'https://***@h/r.git/'"),
        ("https://gitlab.com/x.git", "https://gitlab.com/x.git"),
    ],
)
def test_remote_urls_are_redacted(raw: str, expected: str) -> None:
    """A token must never reach a log, even inside an unexpected Git error."""
    assert redact_url(raw) == expected


def test_git_errors_carry_a_redacted_url(client: GitClient) -> None:
    """The redaction happens where errors are raised, not at each call site."""
    with pytest.raises(GitError) as excinfo:
        client.run(["push", "https://oauth2:secrettoken@127.0.0.1:1/nope.git", "HEAD:refs/heads/x"])
    assert "secrettoken" not in str(excinfo.value)
    assert "***@" in str(excinfo.value)


# --------------------------------------------------------------------------- staging


def test_only_named_paths_are_staged(client: GitClient, repo: Path) -> None:
    """A stray file in the workspace can never be swept into a commit."""
    wanted = _write(repo, "repository/HRDB/HR/packages/P.sql")
    _write(repo, "stray.txt")

    client.add([wanted])
    assert [p.name for p in client.staged_paths()] == ["P.sql"]


def test_blanket_staging_is_refused(client: GitClient, repo: Path) -> None:
    """`git add -A` and `git add .` are forbidden (docs/architecture.md §11.3)."""
    for blanket in (".", "-A", "--all"):
        with pytest.raises(GitError, match="blanket"):
            client.add([repo / blanket])


def test_paths_outside_the_repository_are_refused(client: GitClient, tmp_path: Path) -> None:
    """Staging must stay inside the working tree."""
    with pytest.raises(GitError, match="outside the repository"):
        client.add([tmp_path / "elsewhere.sql"])


def test_deletions_are_staged(client: GitClient, repo: Path) -> None:
    """Reconciliation's obsolete files are staged as deletions."""
    doomed = _write(repo, "repository/HRDB/HR/packages/OLD.sql")
    client.add([doomed])
    client.commit("add\n")

    client.remove([doomed])
    assert client.staged_changes() == [("D", doomed)]
    assert not doomed.exists()


def test_staging_many_paths_is_batched(client: GitClient, repo: Path) -> None:
    """A full export must not exceed the command-line length limit."""
    paths = [_write(repo, f"repository/HRDB/HR/packages/P{i}.sql") for i in range(450)]
    client.add(paths)
    assert len(client.staged_paths()) == 450


# --------------------------------------------------------------------------- empty commit


def test_nothing_staged_means_no_commit(client: GitClient) -> None:
    """The gate that makes a recompilation-only run silent (§11.4)."""
    assert CommitBuilder(client).has_staged_changes() is False


def test_rewriting_identical_content_stages_nothing(client: GitClient, repo: Path) -> None:
    """Git is the precise gate: identical bytes produce no diff (§7.3, §22.3b).

    This is the whole reason the design needs no content comparer.
    """
    path = _write(repo, "repository/HRDB/HR/packages/P.sql", "CREATE OR REPLACE PACKAGE p;\n")
    client.add([path])
    client.commit("first\n")

    path.write_text("CREATE OR REPLACE PACKAGE p;\n", encoding="utf-8")  # byte-identical
    client.add([path])
    assert client.staged_changes() == []
    assert CommitBuilder(client).has_staged_changes() is False


def test_changed_content_does_stage(client: GitClient, repo: Path) -> None:
    """The counterpart: a real change is seen."""
    path = _write(repo, "repository/HRDB/HR/packages/P.sql", "one\n")
    client.add([path])
    client.commit("first\n")

    path.write_text("two\n", encoding="utf-8")
    client.add([path])
    assert client.staged_changes() == [("M", path)]


# --------------------------------------------------------------------------- message


def _unit(database: str = "HRDB", schema: str = "HR") -> RunUnitResult:
    now = utc_now()
    return RunUnitResult(database=database, schema=schema, started_at=now, finished_at=now)


def _mappers(repo: Path, *databases: str) -> dict[str, FileMapper]:
    return {
        name: FileMapper(repo / "repository" / name, name) for name in databases
    }


def test_commit_message_format(client: GitClient, repo: Path) -> None:
    """Subject and body match docs/requirements.md §9."""
    added = _write(repo, "repository/HRDB/HR/packages/EMP_PKG.sql")
    client.add([added])

    builder = CommitBuilder(client, _mappers(repo, "HRDB"))
    message = builder.build_message(_unit(), previous_sha="abc123")
    lines = message.splitlines()

    assert lines[0].startswith("HRDB.HR||")
    assert lines[0].endswith(f": Updated Oracle objects {SKIP_CI_MARKER}")
    assert "Number of objects  : 1" in message
    assert "Previous commit    : abc123" in message
    assert "  HR.EMP_PKG (PACKAGE) [ADDED]" in message


def test_commit_message_reports_gits_verdict(client: GitClient, repo: Path) -> None:
    """ADDED/MODIFIED/DELETED comes from Git, never from a guess."""
    path = _write(repo, "repository/HRDB/HR/views/V.sql", "one\n")
    client.add([path])
    client.commit("first\n")
    path.write_text("two\n", encoding="utf-8")
    client.add([path])

    message = CommitBuilder(client, _mappers(repo, "HRDB")).build_message(_unit(), "")
    assert "HR.V (VIEW) [MODIFIED]" in message


def test_commit_message_is_deterministic_for_a_change_set(
    client: GitClient, repo: Path
) -> None:
    """Two runs with the same change set produce the same object list."""
    for name in ("B_PKG", "A_PKG", "C_PKG"):
        client.add([_write(repo, f"repository/HRDB/HR/packages/{name}.sql")])

    builder = CommitBuilder(client, _mappers(repo, "HRDB"))
    body = builder.build_message(_unit(), "x").split("Objects:")[1]
    assert body == builder.build_message(_unit(), "x").split("Objects:")[1]
    assert body.index("A_PKG") < body.index("B_PKG") < body.index("C_PKG")


def test_subject_names_the_one_schema_the_commit_is_for(
    client: GitClient, repo: Path
) -> None:
    """One commit belongs to one schema, so the subject names it (§11.5).

    Grouping by database is gone with it: a run touching four schemas now
    produces four commits, and without the schema in the subject their subjects
    would differ only by timestamp.
    """
    client.add([_write(repo, "repository/HRDB/HR/packages/A.sql")])

    builder = CommitBuilder(client, _mappers(repo, "HRDB"))
    message = builder.build_message(_unit("HRDB", "PAYROLL"), "x")

    assert message.startswith("HRDB.PAYROLL||")
    assert "  HRDB:" not in message


def test_state_file_is_not_described_as_an_object(client: GitClient, repo: Path) -> None:
    """A path that maps to no Oracle object is omitted, never guessed at."""
    client.add([_write(repo, "state/execution_state.json", "{}\n")])
    message = CommitBuilder(client, _mappers(repo, "HRDB")).build_message(_unit(), "")
    assert "execution_state" not in message
    assert "Number of objects  : 0" in message


def test_commit_message_survives_awkward_characters(client: GitClient, repo: Path) -> None:
    """The message goes in on stdin, so quotes and dashes cannot be misparsed."""
    client.add([_write(repo, "repository/HRDB/HR/packages/P.sql")])
    client.commit('subject with "quotes" and --flags\n\nbody\n')
    assert '"quotes"' in _git(repo, "log", "-1", "--pretty=%B")


# --------------------------------------------------------------------------- push


def _pusher(client: GitClient, **kwargs) -> Pusher:
    return Pusher(client, _settings(), RetrySettings(push_attempts=3, push_backoff_seconds=0.01), **kwargs)


class NoTokenProvider:
    """A secret provider with no push token, as on a local run."""

    def resolve(self, name: str) -> str:
        raise RuntimeError(f"{name} is not set")


def test_push_reaches_the_remote(client: GitClient, repo: Path, remote: Path) -> None:
    """The happy path: commit, push, remote advances."""
    client.add([_write(repo, "repository/HRDB/HR/packages/P.sql")])
    sha = client.commit("add P\n")
    _pusher(client, secret_provider=NoTokenProvider()).push()
    assert _git(remote, "rev-parse", "refs/heads/main") == sha


def test_non_fast_forward_is_resolved_by_rebase_and_retry(
    client: GitClient, repo: Path, remote: Path, tmp_path: Path
) -> None:
    """A concurrent pipeline pushed first; we fetch, rebase, and retry (§11.6)."""
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "clone", str(remote), ".")
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.invalid")
    (other / "concurrent.txt").write_text("theirs\n", encoding="utf-8")
    _git(other, "add", "concurrent.txt")
    _git(other, "commit", "-m", "concurrent push")
    _git(other, "push", "origin", "main")

    client.add([_write(repo, "repository/HRDB/HR/packages/P.sql")])
    client.commit("ours\n")
    _pusher(client, secret_provider=NoTokenProvider()).push()

    log = _git(remote, "log", "--pretty=%s", "refs/heads/main")
    assert "ours" in log and "concurrent push" in log


@pytest.mark.parametrize(
    "message",
    [
        "! [rejected] main -> main (non-fast-forward)",
        "hint: Updates were rejected... fetch first",
        "fatal: unable to access 'https://h/r.git/': Could not resolve host",
        "error: RPC failed; curl 92",
    ],
)
def test_transient_push_failures_are_retried(message: str) -> None:
    """A concurrent push or a network blip is worth another attempt."""
    assert is_retryable_push_failure(PushError(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "fatal: Authentication failed for 'https://gitlab.com/x.git/'",
        "remote: HTTP Basic: Access denied",
        "fatal: could not read Username for 'https://gitlab.com'",
        "remote: 403 Forbidden",
        "fatal: unable to access 'https://h/r.git/': The requested URL returned error: 401 Unauthorized",
    ],
)
def test_authentication_failures_are_never_retried(message: str) -> None:
    """A revoked or wrongly scoped token fails identically every time."""
    assert is_retryable_push_failure(PushError(message)) is False


def test_push_failure_raises_push_error(client: GitClient, repo: Path) -> None:
    """An unreachable remote ends the execution rather than passing silently."""
    client.run(["remote", "set-url", "origin", "https://127.0.0.1:1/nope.git"])
    client.add([_write(repo, "repository/HRDB/HR/packages/P.sql")])
    client.commit("add\n")

    with pytest.raises(PushError):
        Pusher(
            client, _settings(), RetrySettings(push_attempts=1), NoTokenProvider()
        ).push()


def test_token_is_injected_at_call_time_and_never_persisted(
    client: GitClient, repo: Path
) -> None:
    """The token must not survive into .git/config or a CI artifact (§11.6)."""

    class TokenProvider:
        def resolve(self, name: str) -> str:
            return "glpat-averylongtokenvalue"

    client.run(["remote", "set-url", "origin", "https://gitlab.example/group/project.git"])
    url = Pusher(client, _settings(), RetrySettings(), TokenProvider()).remote_url()

    assert url.startswith("https://oauth2:glpat-averylongtokenvalue@gitlab.example/")
    assert "glpat" not in (repo / ".git" / "config").read_text(encoding="utf-8")


def test_state_amend_is_pushed_under_a_lease(
    client: GitClient, repo: Path, remote: Path
) -> None:
    """Decision D-3: one commit per run, amended and force-pushed with a lease."""
    client.add([_write(repo, "repository/HRDB/HR/packages/P.sql")])
    client.commit("first\n")
    pusher = _pusher(client, secret_provider=NoTokenProvider())
    pushed = pusher.push()

    _write(repo, "state/execution_state.json", "{}\n")
    client.add([repo / "state" / "execution_state.json"])
    amended = client.commit("first (amended)\n", amend=True)
    pusher.push(replacing=pushed)

    assert _git(remote, "rev-parse", "refs/heads/main") == amended
    assert len(_git(remote, "log", "--pretty=%H", "refs/heads/main").splitlines()) == 2


def test_the_lease_refuses_to_clobber_a_concurrent_push(
    client: GitClient, repo: Path, remote: Path, tmp_path: Path
) -> None:
    """The whole point of the lease: someone else's commit is never discarded.

    A bare `--force` would silently destroy the concurrent commit here.
    """
    client.add([_write(repo, "repository/HRDB/HR/packages/P.sql")])
    client.commit("ours\n")
    pusher = _pusher(client, secret_provider=NoTokenProvider())
    pushed = pusher.push()

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "clone", str(remote), ".")
    _git(other, "config", "user.name", "Other")
    _git(other, "config", "user.email", "other@example.invalid")
    (other / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _git(other, "add", "theirs.txt")
    _git(other, "commit", "-m", "concurrent")
    _git(other, "push", "origin", "main")
    theirs = _git(remote, "rev-parse", "refs/heads/main")

    client.add([_write(repo, "state/execution_state.json", "{}\n")])
    client.commit("ours (amended)\n", amend=True)
    with pytest.raises(PushError):
        pusher.push(replacing=pushed)

    assert _git(remote, "rev-parse", "refs/heads/main") == theirs


def test_no_module_uses_a_bare_force_push() -> None:
    """Decision D-3: `--force-with-lease`, never a bare `--force`."""
    src_root = Path(__file__).resolve().parent.parent / "src"
    offenders = [
        path.name
        for path in src_root.rglob("*.py")
        for text in [path.read_text(encoding="utf-8")]
        if '"--force"' in text or "'--force'" in text or '"-f"' in text
    ]
    assert not offenders


# --------------------------------------------------------------------------- branch


def test_detached_head_is_put_back_on_the_branch(client: GitClient, repo: Path) -> None:
    """GitLab CI checks out a detached HEAD, which cannot be pushed from."""
    _git(repo, "checkout", "--detach", "HEAD")
    assert client.current_branch() == ""

    client.ensure_branch("main")
    assert client.current_branch() == "main"


def test_ensure_branch_is_a_no_op_when_already_correct(client: GitClient) -> None:
    """Nothing is disturbed when the checkout is already right."""
    before = client.rev_parse("HEAD")
    client.ensure_branch("main")
    assert client.rev_parse("HEAD") == before


# --------------------------------------------------------------------------- remote_head / commit_message


def test_remote_head_matches_a_real_push(client: GitClient, repo: Path, remote: Path) -> None:
    """The whole point: this must reflect the remote's actual state, not a cache."""
    client.add([_write(repo, "a.sql")])
    pushed = client.commit("add a\n")
    _git(repo, "push", "origin", "main")
    assert client.remote_head("origin", "main") == pushed


def test_remote_head_is_empty_for_a_branch_that_does_not_exist(client: GitClient) -> None:
    """A not-yet-pushed branch must not be confused with a connectivity failure."""
    assert client.remote_head("origin", "no-such-branch") == ""


def test_remote_head_raises_when_the_remote_is_unreachable(client: GitClient) -> None:
    """Cannot reach the remote at all is different from "branch absent"."""
    client.run(["remote", "set-url", "origin", "https://127.0.0.1:1/nope.git"])
    with pytest.raises(GitError):
        client.remote_head("origin", "main")


def test_commit_message_round_trips_verbatim(client: GitClient, repo: Path) -> None:
    """The exact text is needed to preserve a stranded commit's message."""
    client.add([_write(repo, "a.sql")])
    client.commit("subject line\n\nbody with detail\n")
    assert client.commit_message("HEAD") == "subject line\n\nbody with detail"
