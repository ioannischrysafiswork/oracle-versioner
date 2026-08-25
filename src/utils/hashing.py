"""Content hashing (docs/architecture.md §2).

Used for logging, reporting, and reconciliation bookkeeping only. It is
explicitly **not** a change-detection mechanism: ``LAST_DDL_TIME`` decides what
is extracted and ``git status`` decides what is committed (§7.3). Introducing a
content comparison would require owner approval (CLAUDE.md §8.1).
"""

from __future__ import annotations

import hashlib

__all__ = ["content_hash"]


def content_hash(text: str) -> str:
    """Return a stable hex digest of ``text``.

    Stable across platforms and interpreter runs: the input is encoded as UTF-8
    before hashing, so the digest never depends on the runner's locale or on
    hash randomization.

    Args:
        text: The normalized source to fingerprint.

    Returns:
        A lowercase SHA-256 hex digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
