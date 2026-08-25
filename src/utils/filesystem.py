r"""Filesystem helpers: atomic write, directory ensure.

Atomicity matters: an interrupted execution must never leave a half-written
``.sql`` file or a truncated ``execution_state.json``
(docs/architecture.md §14.4). Every write goes to a temporary file in the *same
directory* — so the rename stays on one filesystem and is therefore atomic —
and only then replaces the target.

Encoding is UTF-8 without BOM and newlines are written through untranslated, so
LF survives on Windows (§10.2). ``newline=""`` is deliberate: the default would
rewrite ``\\n`` to ``\\r\\n`` on Windows and produce phantom diffs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["atomic_write_text", "ensure_directory"]


def ensure_directory(path: Path) -> Path:
    """Create ``path`` and its parents if they do not exist.

    Args:
        path: The directory to create.

    Returns:
        The same path, for chaining.

    Raises:
        OSError: If the directory cannot be created.
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, as UTF-8 with LF endings.

    A reader never observes partial content: either the previous file or the
    complete new one. The parent directory is created if needed.

    Args:
        path: Destination file.
        text: Content to write.

    Raises:
        OSError: If the write or the replace fails.
    """
    ensure_directory(path.parent)

    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
