"""The Git phase (docs/architecture.md §1.2, §11).

Runs **once per schema**, immediately after that schema's extraction, and is
single-threaded: the working tree and the index are one shared mutable resource
(§1.3, decision D-16).

The ordering is decision D-3 as amended by D-17, and it is the single most
important rule in the design::

    stage this schema's written files + its reconciliation deletions
    nothing staged        -> no commit, no push, watermark untouched
    commit -> push        -> if this fails, nothing has been persisted
    write this schema's state -> amend -> force-push under a lease

A schema's watermark can therefore never advance past changes that did not
reach GitLab, and a schema that fails leaves every earlier schema's work
already pushed and recorded — which is what makes stopping the execution at the
first failure (D-17) lose nothing.

**Recovering a commit stranded by an earlier failed push.** If a previous
execution committed locally and then failed to push, that commit sits on
``HEAD`` indefinitely. Architecture §22.7 assumes the *next* run will naturally
re-extract the same objects and produce a fresh diff to push — but that is
false whenever the re-extracted content is byte-identical to what is already
committed, which is the common case. :meth:`GitPhase.push_stranded_commit`
handles it, and the pipeline calls it **once per execution** before any schema
runs, rather than once per schema: it costs a network round trip, and a commit
stranded by a previous run is a property of the repository, not of any one
schema.
"""

from __future__ import annotations

from pathlib import Path

from src.config.models import AppConfig, GitSettings
from src.config.secrets import SecretProvider
from src.exceptions import GitError
from src.git.commit import CommitBuilder
from src.git.git_client import GitClient
from src.git.push import Pusher, is_retryable_push_failure
from src.logging.logger import get_logger
from src.models.execution import RunUnitResult
from src.repository.file_mapper import FileMapper
from src.state.execution_state import ExecutionStateStore
from src.state.models import ExecutionState
from src.utils.retry import RetryPolicy, retry_call
from src.utils.timers import utc_now

__all__ = ["GitPhase"]

_log = get_logger(__name__)


class GitPhase:
    """Stages, commits, pushes one schema, and only then advances its watermark."""

    def __init__(
        self,
        config: AppConfig,
        state: ExecutionState,
        state_store: ExecutionStateStore,
        mappers: dict[str, FileMapper],
        client: GitClient,
        secret_provider: SecretProvider | None = None,
    ) -> None:
        """Create the Git phase.

        Args:
            config: The validated configuration for this execution.
            state: The state loaded before the run started.
            state_store: Where state is persisted — only after a successful push.
            mappers: Per-database path mappers, for describing the staged set.
            client: Drives ``git``.
            secret_provider: Resolves the push token at call time.
        """
        self.config = config
        self.state = state
        self.state_store = state_store
        self.mappers = mappers
        self.client = client
        self.secret_provider = secret_provider

    @property
    def suppressed(self) -> bool:
        """Whether this execution writes no commits at all."""
        return bool(self.config.dry_run or self.config.no_git)

    def prepare(self) -> None:
        """Put the checkout on a real branch and set the commit identity.

        Done once per execution rather than once per schema: neither operation
        depends on which schema is being committed.
        """
        if self.suppressed:
            return
        git = self.config.settings.git
        self.client.ensure_branch(git.branch)
        self.client.configure_identity(git.author_name, git.author_email)

    def push_stranded_commit(self) -> str | None:
        """Push a commit left unpushed by an earlier execution, if there is one.

        Called once per execution, before any schema is processed, so that a
        stranded commit is republished even when this run produces no new
        changes at all.

        Returns:
            The SHA pushed, or ``None`` when HEAD already matches the remote.

        Raises:
            src.exceptions.GitError: If the remote is unreachable, or the push
                fails.
        """
        if self.suppressed:
            return None

        git = self.config.settings.git
        local_sha = self.client.rev_parse("HEAD")
        if local_sha == self._remote_head_with_retry(git):
            return None

        _log.info("HEAD has an unpushed commit from a previous run; pushing it first")
        pusher = Pusher(self.client, git, self.config.settings.retry, self.secret_provider)
        pushed = pusher.push()
        _log.info("pushed stranded commit %s to %s", pushed, git.branch)
        return pushed

    def run(self, unit: RunUnitResult) -> bool:
        """Commit and push one schema, then advance that schema's watermark.

        Args:
            unit: The completed run unit, updated in place with the commit SHA
                and push outcome.

        Returns:
            ``True`` when a commit was created, or when the phase was
            deliberately suppressed. ``False`` means this schema produced no
            effective change.

        Raises:
            src.exceptions.GitError: On commit or push failure. The caller
                stops the execution (decision D-17); this schema's watermark is
                left untouched.
        """
        if self.suppressed:
            _log.info(
                "git phase suppressed for %s.%s (dry_run=%s, no_git=%s)",
                unit.database, unit.schema, self.config.dry_run, self.config.no_git,
            )
            return True

        if not (unit.written_paths or unit.deleted_paths):
            _log.info(
                "%s.%s: nothing written or deleted; no commit, watermark untouched",
                unit.database, unit.schema,
            )
            return False

        git = self.config.settings.git
        local_sha = self.client.rev_parse("HEAD")
        builder = CommitBuilder(self.client, self.mappers)

        try:
            CommitBuilder.stage(self.client, unit.written_paths, unit.deleted_paths)

            # The empty-commit gate (§11.4). Files were written, but if they
            # are byte-identical to what is committed, Git reports nothing
            # staged — which is exactly how a recompilation-only run stays
            # silent, per schema.
            if not builder.has_staged_changes():
                _log.info(
                    "%s.%s: no effective change; no commit, no push, and the watermark "
                    "is left untouched",
                    unit.database, unit.schema,
                )
                return False

            unit.finished_at = utc_now()
            # Built once, here, while this schema's DDL changes are still in
            # the index. The amend in _persist_state re-uses this text:
            # rebuilding it there would describe an index holding nothing but
            # the state file, and since the amend replaces the message, the
            # object list would vanish from the commit that is published.
            message = builder.build_message(unit, local_sha)
            self.client.commit(message)
        except GitError:
            # Leave no half-staged index behind, so the next execution starts
            # from a clean working tree (§14.4). The reset is `--mixed`: it
            # unstages without discarding extracted files.
            self.client.run(["reset"], check=False)
            raise

        pusher = Pusher(self.client, git, self.config.settings.retry, self.secret_provider)
        pushed_sha = pusher.push()
        unit.commit_sha = pushed_sha
        unit.pushed = True
        _log.info("%s.%s: pushed %s to %s", unit.database, unit.schema, pushed_sha, git.branch)

        self._persist_state(unit, message, pusher, pushed_sha)
        return True

    def _remote_head_with_retry(self, git: GitSettings) -> str:
        """:meth:`GitClient.remote_head`, retried with the push retry policy.

        Args:
            git: Branch, remote, and retry settings.

        Returns:
            The SHA ``git.branch`` points to on ``git.remote``, or ``""``.

        Raises:
            src.exceptions.GitError: If the remote is still unreachable once
                the retry budget is exhausted.
        """
        retry = self.config.settings.retry
        policy = RetryPolicy(
            attempts=retry.push_attempts,
            base_delay=retry.push_backoff_seconds,
            jitter_ratio=retry.jitter_ratio,
        )
        return retry_call(
            lambda: self.client.remote_head(git.remote, git.branch),
            policy=policy,
            is_retryable=is_retryable_push_failure,
            description="check remote HEAD",
            on_retry=lambda attempt, delay, exc: _log.warning(
                "checking remote HEAD attempt %d failed (%s); retrying in %.1fs",
                attempt, exc, delay,
            ),
        )

    def _persist_state(
        self,
        unit: RunUnitResult,
        message: str,
        pusher: Pusher,
        pushed_sha: str,
    ) -> None:
        """Write this schema's state, amend it in, and force-push under a lease.

        Only reached after a successful push, so a watermark can never advance
        past unpushed work.

        Args:
            unit: The completed run unit for this schema.
            message: The message built while the DDL changes were staged; the
                amend must re-use it rather than rebuild it.
            pusher: Performs the lease-protected force push.
            pushed_sha: The only commit the lease permits this amend to replace.
        """
        self.state = self.state.advanced_by(unit, pushed_sha)
        self.state_store.save(self.state)

        self.client.add([Path(self.state_store.path)])
        unit.commit_sha = self.client.commit(message, amend=True)
        pusher.push(replacing=pushed_sha)
        _log.info(
            "%s.%s: state committed and pushed as %s",
            unit.database, unit.schema, unit.commit_sha,
        )
