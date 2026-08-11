"""The Git phase (docs/architecture.md §1.2, §11).

Serialized, single-threaded, and run once per execution *after* all extraction
has finished: the working tree and the index are one shared mutable resource
(§1.3).

The ordering here is decision D-3, and it is the single most important rule in
the design::

    stage written files + reconciliation deletions
    nothing staged        -> exit 0, no commit, no push, no state update
    commit -> push        -> if this fails, nothing has been persisted
    write state -> amend -> force-push under a lease

A watermark can therefore never advance past changes that did not reach GitLab.

Extracted from :mod:`src.workflow.pipeline` so that module orchestrates the
extraction phase and delegates this one, mirroring the two stages §1.2 defines.
**Not in the approved structure** (CLAUDE.md §3); see the Phase 6 report.
"""

from __future__ import annotations

from pathlib import Path

from src.config.models import AppConfig
from src.config.secrets import SecretProvider
from src.exceptions import GitError
from src.git.commit import CommitBuilder
from src.git.git_client import GitClient
from src.git.push import Pusher
from src.logging.logger import get_logger
from src.models.execution import ExecutionResult
from src.repository.file_mapper import FileMapper
from src.state.execution_state import ExecutionStateStore
from src.state.models import ExecutionState
from src.utils.timers import utc_now

__all__ = ["GitPhase"]

_log = get_logger(__name__)


class GitPhase:
    """Stages, commits, pushes, and only then advances state."""

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

    def run(self, result: ExecutionResult) -> bool:
        """Commit and push this execution's changes.

        Args:
            result: Updated in place with the commit SHA and push outcome.

        Returns:
            ``True`` when a commit was created, or when the phase was
            deliberately suppressed. ``False`` means nothing changed, which the
            caller reports as ``NO_CHANGES``.

        Raises:
            src.exceptions.GitError: On commit or push failure (execution-fatal).
        """
        if self.config.dry_run or self.config.no_git:
            _log.info(
                "git phase suppressed (dry_run=%s, no_git=%s)",
                self.config.dry_run, self.config.no_git,
            )
            return True

        written = [path for unit in result.run_units for path in unit.written_paths]
        deleted = [path for unit in result.run_units for path in unit.deleted_paths]
        if not written and not deleted:
            _log.info("nothing to stage; no commit, no push, and state is left untouched")
            return False

        git = self.config.settings.git
        self.client.ensure_branch(git.branch)
        self.client.configure_identity(git.author_name, git.author_email)

        builder = CommitBuilder(self.client, self.mappers)
        try:
            CommitBuilder.stage(self.client, written, deleted)

            # The empty-commit gate (§11.4). Files were written, but if they are
            # byte-identical to what is committed, Git reports nothing staged —
            # which is exactly how a recompilation-only run stays silent.
            if not builder.has_staged_changes():
                _log.info("no effective change; no commit, no push, and state is left untouched")
                return False

            previous_sha = self.client.rev_parse("HEAD")
            result.finished_at = utc_now()
            # Built once, here, while the DDL changes are still in the index.
            # The amend below re-uses this text: rebuilding it there would
            # describe an index holding nothing but the state file, and since
            # the amend replaces the message, the object list would vanish from
            # the commit that is actually published.
            message = builder.build_message(result, previous_sha)
            self.client.commit(message)
        except GitError:
            # Leave no half-staged index behind, so the next execution starts
            # from a clean working tree (§14.4). The reset is `--mixed`: it
            # unstages without discarding the extracted files.
            self.client.run(["reset"], check=False)
            raise

        pusher = Pusher(self.client, git, self.config.settings.retry, self.secret_provider)
        pushed_sha = pusher.push()
        result.commit_sha = pushed_sha
        result.pushed = True
        _log.info("pushed %s to %s (previous %s)", pushed_sha, git.branch, previous_sha or "-")

        self._persist_state(result, message, pusher, pushed_sha)
        return True

    def _persist_state(
        self,
        result: ExecutionResult,
        message: str,
        pusher: Pusher,
        pushed_sha: str,
    ) -> None:
        """Write state, amend it into the commit, and force-push under a lease.

        Only reached after a successful push, so state can never advance past
        unpushed work.

        Args:
            result: The completed execution.
            message: The message built while the DDL changes were staged; the
                amend must re-use it rather than rebuild it.
            pusher: Performs the lease-protected force push.
            pushed_sha: The only commit the lease permits this amend to replace.
        """
        self.state = self.state.advanced_by(result.run_units, result.commit_sha)
        self.state_store.save(self.state)

        self.client.add([Path(self.state_store.path)])
        result.commit_sha = self.client.commit(message, amend=True)
        pusher.push(replacing=pushed_sha)
        _log.info("state committed and pushed as %s", result.commit_sha)
