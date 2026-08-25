"""Thin wrapper over ``git`` invocations (docs/architecture.md §11.1).

Rationale for the CLI over a library (decision D-4): no extra dependency, exact
and auditable command lines, and predictable behaviour on the detached HEAD
that GitLab CI produces.

Two rules are enforced here rather than trusted to call sites:

* **Staging is always by explicit path.** :meth:`GitClient.add` refuses ``-A``,
  ``--all``, and ``.``, so a stray file in the workspace can never be swept
  into a commit (§11.3).
* **Credentials never reach a log.** Every command line and every error message
  passes through :func:`redact_url` before it is logged or raised, so a remote
  URL carrying a token is redacted even inside a Git error nobody anticipated.

``GIT_TERMINAL_PROMPT=0`` is set on every invocation: an unattended run must
fail fast on a missing credential rather than block forever on a prompt.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from src.exceptions import GitError
from src.logging.logger import get_logger

__all__ = ["GitClient", "redact_url"]

_log = get_logger(__name__)

#: Matches the ``user:token@`` portion of an HTTP(S) remote URL.
_CREDENTIAL_URL = re.compile(r"(https?://)[^/\s@]+@")

#: Arguments that would stage more than the paths this execution touched.
_BLANKET_STAGING = frozenset({"-A", "--all", ".", "-u", "--update", ":/"})


def redact_url(text: str) -> str:
    """Replace embedded credentials in any URL inside ``text``.

    Args:
        text: A command line, output, or error message.

    Returns:
        The text with ``https://user:token@host`` reduced to ``https://***@host``.
    """
    return _CREDENTIAL_URL.sub(r"\1***@", text)


class GitClient:
    """Runs ``git`` subcommands in a working tree."""

    def __init__(self, work_tree: Path, timeout: int = 300) -> None:
        """Create a client bound to a working tree.

        Args:
            work_tree: The repository root.
            timeout: Per-command timeout in seconds, so a hung network call
                cannot stall the pipeline until the runner's own timeout.
        """
        self.work_tree = work_tree
        self.timeout = timeout

    def run(self, args: Sequence[str], *, check: bool = True) -> str:
        """Run ``git`` with ``args`` and return its stdout.

        Args:
            args: Arguments after ``git``, e.g. ``["status", "--porcelain"]``.
            check: Raise on a non-zero exit status.

        Returns:
            Captured stdout, stripped.

        Raises:
            GitError: On a non-zero exit status when ``check`` is set, on a
                timeout, or if ``git`` is not installed.
        """
        command = ["git", *args]
        _log.debug("git %s", redact_url(" ".join(args)))
        try:
            completed = subprocess.run(  # noqa: S603 - list form, never a shell
                command,
                cwd=self.work_tree,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env={"GIT_TERMINAL_PROMPT": "0", "PATH": _path_env(), "HOME": str(Path.home())},
            )
        except FileNotFoundError as exc:
            raise GitError("git is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {redact_url(args[0])} timed out after {self.timeout}s") from exc

        if check and completed.returncode != 0:
            raise GitError(
                f"git {redact_url(' '.join(args))} failed ({completed.returncode}): "
                f"{redact_url((completed.stderr or completed.stdout).strip())}"
            )
        return completed.stdout.strip()

    # ------------------------------------------------------------------ state

    def rev_parse(self, ref: str = "HEAD") -> str:
        """Return the SHA that ``ref`` resolves to, or ``""`` if it does not."""
        try:
            return self.run(["rev-parse", ref])
        except GitError:
            return ""

    def current_branch(self) -> str:
        """Return the checked-out branch, or ``""`` when HEAD is detached."""
        name = self.run(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return "" if name in ("HEAD", "") else name

    def remote_head(self, remote: str, branch: str) -> str:
        """Return the SHA ``branch`` points to on ``remote``.

        Talks to the remote directly (``ls-remote``) rather than trusting a
        local remote-tracking ref, which is only ever as fresh as the last
        fetch. Used to detect a commit that a previous run made locally but
        never managed to push — see :meth:`~src.workflow.git_phase.GitPhase.run`.

        Args:
            remote: The remote name, e.g. ``origin``.
            branch: The branch name, e.g. ``main``.

        Returns:
            The SHA, or ``""`` if the branch does not exist on the remote yet.

        Raises:
            GitError: If the remote cannot be reached at all.
        """
        output = self.run(["ls-remote", remote, f"refs/heads/{branch}"])
        return output.split()[0] if output else ""

    def commit_message(self, ref: str = "HEAD") -> str:
        """Return the full commit message of ``ref``, verbatim."""
        return self.run(["log", "-1", "--pretty=%B", ref])

    def ensure_branch(self, branch: str) -> None:
        """Make sure work happens on ``branch`` rather than a detached HEAD.

        GitLab CI checks out a detached HEAD, which cannot be pushed from. The
        branch is pointed at the current commit; the remote is deliberately
        *not* fast-forwarded here — see the Phase 6 report.

        Args:
            branch: The target branch name.

        Raises:
            GitError: If the branch cannot be created or checked out.
        """
        if self.current_branch() == branch:
            return
        _log.info("HEAD is detached or on another branch; checking out %s", branch)
        self.run(["checkout", "-B", branch])

    # ------------------------------------------------------------------ index

    def add(self, paths: Sequence[Path]) -> None:
        """Stage the given paths explicitly.

        Args:
            paths: Files written by this execution.

        Raises:
            GitError: If a path looks like a blanket-staging argument, or the
                command fails.
        """
        self._stage(["add", "--"], paths)

    def remove(self, paths: Sequence[Path]) -> None:
        """Stage deletions for the given paths.

        Args:
            paths: Files reconciliation determined are obsolete.

        Raises:
            GitError: If a path looks like a blanket-staging argument, or the
                command fails.
        """
        self._stage(["rm", "--quiet", "--"], paths)

    def _stage(self, prefix: list[str], paths: Sequence[Path]) -> None:
        """Run a staging command over ``paths``, in batches, by explicit path."""
        if not paths:
            return
        arguments = [self._relative(path) for path in paths]
        for argument in arguments:
            if argument in _BLANKET_STAGING:
                raise GitError(f"refusing to stage with a blanket argument: {argument!r}")
        # Batched so a full export cannot exceed the command-line length limit.
        for start in range(0, len(arguments), 200):
            self.run([*prefix, *arguments[start : start + 200]])

    def _relative(self, path: Path) -> str:
        """Return ``path`` relative to the working tree, in POSIX form."""
        try:
            return path.resolve().relative_to(self.work_tree.resolve()).as_posix()
        except ValueError as exc:
            raise GitError(f"{path} is outside the repository {self.work_tree}") from exc

    def staged_changes(self) -> list[tuple[str, Path]]:
        """Return the staged set as ``(status, path)`` pairs.

        Returns:
            One entry per staged path, where status is ``A``, ``M``, ``D``, or
            another ``git diff --name-status`` code. An empty list means there
            is nothing to commit — the gate in §11.4.

        Raises:
            GitError: If the command fails.
        """
        output = self.run(["diff", "--cached", "--name-status"])
        changes: list[tuple[str, Path]] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                changes.append((parts[0][0], self.work_tree / parts[-1]))
        return changes

    def staged_paths(self) -> list[Path]:
        """Return the currently staged paths."""
        return [path for _, path in self.staged_changes()]

    # ------------------------------------------------------------------ history

    def commit(self, message: str, *, amend: bool = False) -> str:
        """Create or amend a commit and return its SHA.

        ``--no-verify`` is never used: a hook failure is a real signal.

        Args:
            message: The full commit message.
            amend: Replace the previous commit rather than adding one.

        Returns:
            The resulting commit SHA.

        Raises:
            GitError: If the commit fails.
        """
        args = ["commit", "--file=-", "--cleanup=verbatim"]
        if amend:
            args.append("--amend")
        self._run_with_input(args, message)
        return self.rev_parse("HEAD")

    def _run_with_input(self, args: Sequence[str], stdin_text: str) -> None:
        """Run a git command feeding ``stdin_text`` on standard input.

        The commit message goes in on stdin rather than as an argument, so a
        message containing quotes, newlines, or a stray ``-`` cannot be
        misparsed as options.
        """
        try:
            completed = subprocess.run(  # noqa: S603 - list form, never a shell
                ["git", *args],  # noqa: S607 - git is resolved from PATH by design
                cwd=self.work_tree,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env={"GIT_TERMINAL_PROMPT": "0", "PATH": _path_env(), "HOME": str(Path.home())},
            )
        except FileNotFoundError as exc:
            raise GitError("git is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out after {self.timeout}s") from exc

        if completed.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({completed.returncode}): "
                f"{redact_url((completed.stderr or completed.stdout).strip())}"
            )

    def configure_identity(self, name: str, email: str) -> None:
        """Set the commit author for this repository only."""
        self.run(["config", "user.name", name])
        self.run(["config", "user.email", email])


def _path_env() -> str:
    """Return the ``PATH`` to hand to git, so the executable is findable."""
    import os

    return os.environ.get("PATH", "")
