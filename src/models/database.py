"""Runtime database descriptor (docs/architecture.md §2).

Distinct from :class:`src.config.models.DatabaseConfig`: the config object is
what was read from YAML, this is what a run unit works with once the watermark
has been resolved from ``execution_state.json`` and the candidate watermark has
been read from the *database* clock (docs/architecture.md §4.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

__all__ = ["DatabaseRuntime"]


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    """A single database as seen by its run unit."""

    #: Configured database name.
    name: str
    #: Repository subtree this database writes into.
    git_folder: Path
    #: Already overlap-adjusted and ready to bind into the discovery query.
    #: The raw ``last_successful_run`` stays in ``execution_state.json``.
    watermark: datetime | None
    #: Read from the *database* clock at run-unit start; persisted only after a
    #: successful push.
    candidate_watermark: datetime | None = None

    @property
    def is_full_export(self) -> bool:
        """``True`` when no watermark exists, i.e. every object is in scope."""
        return self.watermark is None
