"""Commit message construction and the empty-commit gate.

Subject (docs/architecture.md §11.5, amending docs/requirements.md §9)::

    <database>.<schema>||<sysdate>: Updated Oracle objects

The schema component follows from one commit per schema (decision D-17).
Without it a run touching four schemas produces four commits whose subjects
differ only by timestamp.

Body::

    Execution time     : <start> -> <end> (<duration>)
    Number of objects  : <n>
    Previous commit    : <sha>
    New execution ts   : <timestamp>

    Objects:
      <OWNER>.<OBJECT_NAME> (<OBJECT_TYPE>) [ADDED|MODIFIED|DELETED]

The object list is ordered by ``OWNER, OBJECT_TYPE, OBJECT_NAME``, so two runs
producing the same change set produce byte-identical bodies. Every object in
one commit belongs to the one schema that commit is for, so no grouping is
needed.

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
from src.models.execution import RunUnitResult
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

    def describe_staged(self) -> list[str]:
        """Describe the staged set as object lines.

        Paths that do not map to an Oracle object — the state file, most
        obviously — are omitted rather than guessed at.

        Returns:
            Sorted ``OWNER.NAME (TYPE) [VERDICT]`` lines.
        """
        lines: list[str] = []
        for status, path in self.client.staged_changes():
            for mapper in self.mappers.values():
                try:
                    obj = mapper.to_object(path)
                except RepositoryError:
                    continue
                verdict = _STATUS_WORDS.get(status, status)
                lines.append(
                    f"  {obj.owner}.{obj.object_name} ({obj.object_type}) [{verdict}]"
                )
                break

        lines.sort()
        return lines

    def build_message(self, unit: RunUnitResult, previous_sha: str) -> str:
        """Render the full commit message for one schema.

        Must be called while that schema's DDL changes are still in the index:
        the amend that adds the state file re-uses this text rather than
        rebuilding it, because rebuilding would describe an index holding
        nothing but the state file and publish an empty object list.

        Args:
            unit: The completed run unit for the schema being committed.
            previous_sha: HEAD before this commit, for the body.

        Returns:
            Subject and body, deterministic for a given change set and carrying
            the ``[skip ci]`` marker.
        """
        lines = self.describe_staged()
        finished = to_utc(unit.finished_at) if unit.finished_at else None
        started = to_utc(unit.started_at) if unit.started_at else None
        stamp = finished or started
        timestamp = stamp.isoformat() if stamp else ""

        subject = (
            f"{unit.database}.{unit.schema}||{timestamp}: "
            f"Updated Oracle objects {SKIP_CI_MARKER}"
        )

        duration = ""
        if started and finished:
            duration = format_duration((finished - started).total_seconds())

        body = [
            "",
            f"Execution time     : {started.isoformat() if started else '-'} -> "
            f"{finished.isoformat() if finished else '-'} ({duration})",
            f"Number of objects  : {len(lines)}",
            f"Previous commit    : {previous_sha or '-'}",
            f"New execution ts   : {timestamp}",
            "",
            "Objects:",
            *lines,
        ]

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
