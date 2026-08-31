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

__all__ = ["DatabaseRuntime", "SchemaRuntime"]


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    """A single database as seen by the execution."""

    #: Configured database name.
    name: str
    #: Repository subtree this database writes into.
    git_folder: Path


@dataclass(frozen=True, slots=True)
class SchemaRuntime:
    """One schema of one database — the unit of work (decision D-15)."""

    #: Configured database name.
    name: str
    #: The Oracle owner this unit processes.
    schema: str
    #: Repository subtree this database writes into; the schema is a folder
    #: beneath it, resolved by the file mapper.
    git_folder: Path
    #: Already overlap-adjusted and ready to bind into the discovery query.
    #: The raw ``last_successful_run`` stays in ``execution_state.json``, and
    #: it is this *schema's* watermark, not the database's.
    watermark: datetime | None
    #: Read from the *database* clock at run-unit start; persisted only after
    #: this schema's own push succeeds.
    candidate_watermark: datetime | None = None

    @property
    def is_full_export(self) -> bool:
        """``True`` when this schema has no watermark, i.e. everything is in scope."""
        return self.watermark is None
