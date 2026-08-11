"""Writing normalized source to the repository (docs/architecture.md §10.2).

Files are UTF-8 without BOM, LF line endings, exactly one trailing newline, and
written atomically so an interrupted run cannot leave a half-written file.

The conventions are enforced *here* as well as in the normalizer. That is not
redundancy for its own sake: the writer is the last thing between the process
and the repository, and a BOM or a stray CRLF reaching disk shows up as a
phantom diff in every future run.

Dry-run suppression is implemented by injecting :class:`NullWriter`, not by
scattering ``if dry_run:`` checks through the business logic — so a dry run
exercises the same code path and there is no branch where a stray write could
survive (§16).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from src.exceptions import RepositoryError
from src.logging.logger import get_logger
from src.utils.filesystem import atomic_write_text

__all__ = ["FileWriter", "NullWriter", "Writer", "enforce_conventions"]

_log = get_logger(__name__)


def enforce_conventions(content: str) -> str:
    """Return ``content`` in the repository's on-disk form.

    LF line endings, no BOM, and exactly one trailing newline.

    Args:
        content: Normalized source.

    Returns:
        The content as it must appear on disk.

    Raises:
        RepositoryError: If the content is empty or whitespace only. Writing
            that would replace a good file with nothing.
    """
    if not content or not content.strip():
        raise RepositoryError("refusing to write empty content")

    text = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    return text.rstrip("\n") + "\n"


class Writer(Protocol):
    """Writes one object's normalized source to its mapped path."""

    def write(self, path: Path, content: str) -> None:
        """Write ``content`` to ``path``."""
        ...


class FileWriter:
    """Atomic, UTF-8/LF writer that creates missing directories."""

    def write(self, path: Path, content: str) -> None:
        """Write ``content`` to ``path`` atomically.

        Missing directories are created, so a newly created schema or object
        type needs no special handling (docs/architecture.md §8).

        Args:
            path: Target file.
            content: Normalized source.

        Raises:
            RepositoryError: If the content is unwritable or the write fails.
        """
        text = enforce_conventions(content)
        try:
            atomic_write_text(path, text)
        except OSError as exc:
            raise RepositoryError(f"cannot write {path}: {exc}") from exc
        _log.debug("wrote %s (%d bytes)", path, len(text.encode("utf-8")))


class NullWriter:
    """Dry-run writer: records the intent, touches nothing."""

    def __init__(self) -> None:
        """Create a writer that remembers what it would have written."""
        self.would_write: list[Path] = []

    def write(self, path: Path, content: str) -> None:
        """Log what would have been written, without touching the filesystem.

        The content is still validated, so a dry run reports the same failures
        a real run would.

        Args:
            path: The path that would have been written.
            content: The content that would have been written; not persisted.

        Raises:
            RepositoryError: If the content would have been unwritable.
        """
        text = enforce_conventions(content)
        self.would_write.append(path)
        _log.info("dry-run: would write %s (%d bytes)", path, len(text.encode("utf-8")))
