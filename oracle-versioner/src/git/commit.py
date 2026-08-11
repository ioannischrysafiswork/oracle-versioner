"""Commit message construction and the empty-commit gate.

Subject (docs/requirements.md §9)::

    <database>||<sysdate>: Updated Oracle objects

Body::

    Execution time     : <start> -> <end> (<duration>)
    Number of objects  : <n>
    Previous commit    : <sha>
    New execution ts   : <timestamp>

    Objects:
      <OWNER>.<OBJECT_NAME> (<OBJECT_TYPE>) [ADDED|MODIFIED|DELETED]

The object list is ordered by ``OWNER, OBJECT_TYPE, OBJECT_NAME`` and grouped
per database, so two runs producing the same change set produce byte-identical
bodies.

The ADDED/MODIFIED/DELETED verdict is Git's, read from
``git diff --cached --name-status`` and mapped back to objects through the
invertible path mapping. The application never guesses at it — Git is the
authority on what actually changed (§7.3).

The empty-commit gate (§11.4): if nothing is staged, the pipeline does not
commit, does not push, and does not update state. That is what makes a
recompilation-only run silent.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from src.exceptions import RepositoryError
from src.git.git_client import GitClient
from src.models.execution import ExecutionResult
from src.repository.file_mapper import FileMapper
from src.utils.timers import format_duration, to_utc

__all__ = ["SKIP_CI_MARKER", "CommitBuilder"]

#: Prevents the agent's own push from triggering a pipeline (§11.7). The
#: `rules:` clause in .gitlab-ci.yml is the second, independent guard.
SKIP_CI_MARKER = "[skip ci]"

_STATUS_WORDS = {"A": "ADDED", "M": "MODIFIED", "D": "DELETED", "R": "RENAMED", "C": "COPIED"}


class CommitBuilder:
    """Builds commit messages and decides whether a commit should happen."""

    def __init__(self, client: GitClient, mappers: dict[str, FileMapper] | None = None) -> None:
        """Create a builder.

        Args:
            client: The Git client for this repository.
            mappers: Database name to its path mapper, used to describe staged
                paths as Oracle objects.
        """
        self.client = client
        self.mappers = mappers or {}

    def has_staged_changes(self) -> bool:
        """Whether anything is staged for commit.

        Returns:
            ``False`` when the execution must exit 0 without committing.
        """
        return bool(self.client.staged_changes())

    def describe_staged(self) -> dict[str, list[str]]:
        """Describe the staged set as object lines, grouped by database.

        Paths that do not map to an Oracle object — the state file, most
        obviously — are omitted rather than guessed at.

        Returns:
            Database name to sorted ``OWNER.NAME (TYPE) [VERDICT]`` lines.
        """
        grouped: dict[str, list[str]] = {}
        for status, path in self.client.staged_changes():
            for database, mapper in self.mappers.items():
                try:
                    obj = mapper.to_object(path)
                except RepositoryError:
                    continue
                verdict = _STATUS_WORDS.get(status, status)
                grouped.setdefault(database, []).append(
                    f"  {obj.owner}.{obj.object_name} ({obj.object_type}) [{verdict}]"
                )
                break

        for lines in grouped.values():
            lines.sort()
        return grouped

    def build_message(self, result: ExecutionResult, previous_sha: str) -> str:
        """Render the full commit message for ``result``.

        Args:
            result: The completed execution.
            previous_sha: HEAD before this commit, for the body.

        Returns:
            Subject and body, deterministic for a given change set and carrying
            the ``[skip ci]`` marker.
        """
        grouped = self.describe_staged()
        databases = sorted(grouped) or sorted(unit.database for unit in result.run_units)
        finished = to_utc(result.finished_at) if result.finished_at else None
        started = to_utc(result.started_at) if result.started_at else None
        stamp = (finished or started)
        timestamp = stamp.isoformat() if stamp else ""

        subject = f"{','.join(databases)}||{timestamp}: Updated Oracle objects {SKIP_CI_MARKER}"

        duration = ""
        if started and finished:
            duration = format_duration((finished - started).total_seconds())
        objects = sum(len(lines) for lines in grouped.values())

        body = [
            "",
            f"Execution time     : {started.isoformat() if started else '-'} -> "
            f"{finished.isoformat() if finished else '-'} ({duration})",
            f"Number of objects  : {objects}",
            f"Previous commit    : {previous_sha or '-'}",
            f"New execution ts   : {timestamp}",
            "",
            "Objects:",
        ]
        for database in databases:
            lines = grouped.get(database, [])
            if len(databases) > 1:
                body.append(f"  {database}:")
                body.extend(f"  {line}" for line in lines)
            else:
                body.extend(lines)

        return subject + "\n" + "\n".join(body) + "\n"

    @staticmethod
    def stage(client: GitClient, written: Iterable[Path], deleted: Iterable[Path]) -> None:
        """Stage exactly the paths this execution touched.

        Args:
            client: The Git client.
            written: Files written during extraction.
            deleted: Files reconciliation found obsolete.

        Raises:
            src.exceptions.GitError: If staging fails.
        """
        client.add(list(written))
        client.remove(list(deleted))
