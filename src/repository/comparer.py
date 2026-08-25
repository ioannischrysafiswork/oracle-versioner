"""Deleted-object reconciliation (docs/architecture.md §9).

**This module compares path sets, not file contents.** There is deliberately no
content-comparison step anywhere in the system: ``LAST_DDL_TIME`` decides what
is extracted and ``git status`` decides what is committed (§7.3). Adding a
content comparison would require owner approval (CLAUDE.md §8.1).

``LAST_DDL_TIME`` cannot detect a drop, because the ``DBA_OBJECTS`` row simply
disappears. Reconciliation resolves that: the full in-scope inventory is mapped
to expected paths, the actual ``.sql`` files under the database subtree are
enumerated, and any file with no matching object is staged for deletion.

Safety rules, because deletion is destructive:

  * skipped entirely if the inventory query failed — a transient permissions or
    connectivity problem must never read as "everything was dropped";
  * blocked for the whole run unit if it would delete more than the configured
    percentage of a database's files (default 10%, decision D-6);
  * never active in dry-run mode;
  * only paths under schemas currently in scope are even considered, so
    removing a schema from the include filter cannot delete its history.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from src.config.models import ReconciliationSettings, SchemaFilter
from src.logging.logger import get_logger
from src.repository.file_mapper import FileMapper

__all__ = ["Reconciler"]

_log = get_logger(__name__)


class Reconciler:
    """Computes the set of repository files with no corresponding object."""

    def __init__(
        self,
        settings: ReconciliationSettings | None = None,
        mapper: FileMapper | None = None,
    ) -> None:
        """Create a reconciler.

        Args:
            settings: Reconciliation policy, including the mass-delete threshold.
            mapper: Maps objects to their expected paths. Required for
                :meth:`obsolete_paths`.
        """
        self.settings = settings or ReconciliationSettings()
        self.mapper = mapper

    def enumerate_paths(self, schemas: SchemaFilter | None = None) -> list[Path]:
        """List the ``.sql`` files under the in-scope schemas of this database.

        Args:
            schemas: The configured include/exclude filter. An empty include
                list means every schema present that is not excluded.

        Returns:
            Sorted paths. Empty when the database subtree does not exist yet,
            which is the normal state before the first export.
        """
        if self.mapper is None:
            raise ValueError("a FileMapper is required to enumerate repository paths")

        root = self.mapper.database_root
        if not root.is_dir():
            return []

        include = {name.upper() for name in (schemas.include if schemas else ())}
        exclude = {name.upper() for name in (schemas.exclude if schemas else ())}

        paths: list[Path] = []
        for schema_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            name = schema_dir.name.upper()
            if include and name not in include:
                continue
            if name in exclude:
                continue
            paths.extend(sorted(schema_dir.rglob(f"*{'.sql'}")))
        return paths

    def obsolete_paths(
        self,
        inventory: Iterable[object],
        existing_paths: Iterable[Path],
    ) -> list[Path]:
        """Return files on disk that no longer correspond to an object.

        A pure set difference — no file is opened and no content is compared.

        Args:
            inventory: The full in-scope object list for the database.
            existing_paths: Every ``.sql`` file under the in-scope subtree.

        Returns:
            Paths to stage for deletion, sorted for determinism.

        Raises:
            ValueError: If no mapper was supplied.
            src.exceptions.RepositoryError: If an inventory object is unmappable.
        """
        if self.mapper is None:
            raise ValueError("a FileMapper is required to reconcile")

        expected = set(self.mapper.map_all(inventory))  # type: ignore[arg-type]
        return sorted(path for path in existing_paths if path not in expected)

    def exceeds_threshold(self, deletions: int, total: int) -> bool:
        """Whether ``deletions`` breaches the mass-deletion safety threshold.

        Args:
            deletions: Number of files reconciliation wants to delete.
            total: Total number of files under the database subtree.

        Returns:
            ``True`` if all deletions must be blocked for this run unit. Always
            ``False`` when the operator passed ``--allow-mass-delete``, and
            always ``False`` when there is nothing to delete.
        """
        if not deletions or self.settings.allow_mass_delete:
            return False
        if not total:
            # Nothing on disk, yet something to delete: the caller's inputs
            # disagree. Refuse rather than guess.
            return True
        return (deletions / total) * 100 > self.settings.mass_delete_threshold_percent
