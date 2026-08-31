"""Loading and saving ``execution_state.json`` (docs/architecture.md §11.9, §14.3).

Two rules govern this module, and both are load-bearing:

1. **State is never written before the push succeeds.** The watermark must
   never advance past changes that did not reach GitLab. This module cannot
   enforce that on its own — it is the Git phase that decides *when* to call
   :meth:`ExecutionStateStore.save` — but :attr:`ExecutionStateStore.saved`
   records whether a save happened, so the rule is testable.
2. **A corrupt state file is fatal, never recreated.** Silently recreating it
   would look like "no history", trigger a full export, and mask data loss.
   Exit code 2.

Writes are atomic, so the previous state is never lost.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.exceptions import StateError
from src.logging.logger import get_logger
from src.state.models import (
    LEGACY_STATE_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    ExecutionState,
)
from src.utils.filesystem import atomic_write_text

__all__ = ["ExecutionStateStore"]

_log = get_logger(__name__)


class ExecutionStateStore:
    """Reads and writes the execution state file."""

    def __init__(self, path: Path) -> None:
        """Create a store bound to a state file.

        Args:
            path: Path to ``execution_state.json``.
        """
        self.path = path
        self.saved = False

    def load(self) -> ExecutionState:
        """Load the state file.

        Returns:
            The parsed state, or an empty state when the file does not exist —
            which means a full export for every database.

        Raises:
            StateError: If the file exists but cannot be read, parsed, or
                validated. Never recovered from by recreating the file.
        """
        if not self.path.exists():
            _log.info("no state file at %s; every schema performs a full export", self.path)
            return ExecutionState()

        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateError(f"cannot read state file {self.path}: {exc}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateError(f"state file {self.path} is not valid JSON: {exc}") from exc

        if isinstance(payload, dict) and payload.get("schema_version") == (
            LEGACY_STATE_SCHEMA_VERSION
        ):
            # Announced loudly rather than silently: this run re-exports every
            # schema in full, which is the intended effect of D-18 but looks
            # alarming in the log if it is not explained.
            _log.warning(
                "state file %s is schema_version %d; its per-database watermarks cannot be "
                "attributed to any schema and are discarded. Every schema performs one full "
                "export, after which state is written at schema_version %d "
                "(docs/architecture.md §4.4, decision D-18).",
                self.path,
                LEGACY_STATE_SCHEMA_VERSION,
                STATE_SCHEMA_VERSION,
            )

        state = ExecutionState.from_dict(payload)
        schemas = sum(len(db.schemas) for db in state.databases.values())
        _log.info(
            "loaded state for %d schema(s) across %d database(s) from %s",
            schemas,
            len(state.databases),
            self.path,
        )
        return state

    def save(self, state: ExecutionState) -> None:
        """Write the state file atomically.

        Only ever called after a successful push.

        Args:
            state: The state to persist.

        Raises:
            StateError: If the write fails.
        """
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=False, ensure_ascii=False)
        try:
            atomic_write_text(self.path, payload + "\n")
        except OSError as exc:
            raise StateError(f"cannot write state file {self.path}: {exc}") from exc

        self.saved = True
        schemas = sum(len(db.schemas) for db in state.databases.values())
        _log.info(
            "wrote state for %d schema(s) across %d database(s) to %s",
            schemas,
            len(state.databases),
            self.path,
        )
