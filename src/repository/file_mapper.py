"""Object to path mapping (docs/architecture.md §10).

    repository/<DATABASE>/<SCHEMA>/<type_folder>/<OBJECT_NAME>.sql

The mapping is total and **injective**: one object maps to exactly one path and
one path maps back to exactly one object. Deleted-object reconciliation depends
on that invertibility — it decides what to delete purely by differencing path
sets — which is why an unmappable name fails loudly instead of being encoded
(decision D-5). Silent encoding would break the inverse and, sooner or later,
delete the wrong file.

A mapper is scoped to one database's subtree, because the repository folder is
configurable per database and need not equal the database name.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from src.exceptions import RepositoryError
from src.models.oracle_object import ObjectType, OracleObject

__all__ = [
    "FOLDER_BY_TYPE",
    "ILLEGAL_NAME_CHARACTERS",
    "TYPE_BY_FOLDER",
    "WINDOWS_RESERVED_NAMES",
    "FileMapper",
]

#: Object type to repository folder (docs/architecture.md §10.1).
FOLDER_BY_TYPE: dict[ObjectType, str] = {
    ObjectType.PACKAGE: "packages",
    ObjectType.PACKAGE_BODY: "package_bodies",
    ObjectType.PROCEDURE: "procedures",
    ObjectType.FUNCTION: "functions",
    ObjectType.VIEW: "views",
    ObjectType.TABLE: "tables",
    ObjectType.TYPE: "types",
    ObjectType.TYPE_BODY: "type_bodies",
    ObjectType.TRIGGER: "triggers",
    ObjectType.SEQUENCE: "sequences",
    ObjectType.SYNONYM: "synonyms",
    ObjectType.MATERIALIZED_VIEW: "materialized_views",
}

#: The inverse of :data:`FOLDER_BY_TYPE`. Its existence is the mapping's
#: invertibility, so the two must always stay in step.
TYPE_BY_FOLDER: dict[str, ObjectType] = {
    folder: object_type for object_type, folder in FOLDER_BY_TYPE.items()
}

#: Characters that make an object name unmappable; the object fails loudly (D-5).
ILLEGAL_NAME_CHARACTERS: frozenset[str] = frozenset('/\\:*?"<>|')

#: Windows cannot create a file with these stems, whatever the extension. A
#: repository containing one cannot be checked out on Windows at all — not just
#: that one file — so an object named ``CON`` is failed rather than committed.
#: This goes beyond the character list in §10.3; see the Phase 5 report.
WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{digit}" for digit in range(1, 10)]
    + [f"LPT{digit}" for digit in range(1, 10)]
)

_EXTENSION = ".sql"


class FileMapper:
    """Maps objects to repository paths and back, within one database subtree."""

    def __init__(self, database_root: Path, database: str) -> None:
        """Create a mapper.

        Args:
            database_root: The database's subtree, ``repository/<git_folder>``.
            database: The configured database name, restored by :meth:`to_object`.
        """
        self.database_root = database_root
        self.database = database

    @staticmethod
    def validate_name(name: str, kind: str) -> str:
        """Reject a name that cannot be represented as a path component.

        Args:
            name: The schema or object name, exactly as Oracle reports it.
            kind: ``schema`` or ``object``, for the error message.

        Returns:
            The name unchanged — it is never rewritten.

        Raises:
            RepositoryError: If the name is empty, contains an illegal or
                control character, ends in a dot or space, or is a Windows
                reserved device name.
        """
        if not name or not name.strip():
            raise RepositoryError(f"{kind} name is empty")

        illegal = sorted(ILLEGAL_NAME_CHARACTERS & set(name))
        if illegal:
            raise RepositoryError(f"{kind} name {name!r} contains illegal character(s) {illegal}")
        if any(char < " " or char == "\x7f" for char in name):
            raise RepositoryError(f"{kind} name {name!r} contains a control character")
        if name[-1] in ". ":
            raise RepositoryError(f"{kind} name {name!r} ends with a dot or space")
        if name.upper() in WINDOWS_RESERVED_NAMES:
            raise RepositoryError(f"{kind} name {name!r} is a reserved device name on Windows")
        return name

    def to_path(self, obj: OracleObject) -> Path:
        """Return the single ``.sql`` path that ``obj`` maps to.

        Args:
            obj: The object to map.

        Returns:
            The path, below this mapper's database root.

        Raises:
            RepositoryError: If the schema or object name is unmappable, or the
                object type has no folder.
        """
        try:
            folder = FOLDER_BY_TYPE[obj.object_type]
        except KeyError as exc:
            raise RepositoryError(f"no repository folder for object type {obj.object_type}") from exc

        owner = self.validate_name(obj.owner, "schema")
        name = self.validate_name(obj.object_name, "object")
        return self.database_root / owner / folder / f"{name}{_EXTENSION}"

    def to_object(self, path: Path) -> OracleObject:
        """Invert :meth:`to_path`.

        Args:
            path: A ``.sql`` path under this mapper's database root.

        Returns:
            The object that path represents.

        Raises:
            RepositoryError: If the path is not a valid mapping.
        """
        # Both sides are resolved first: the mapper's root is configured
        # relative to the working directory, while Git reports staged paths
        # absolute. Comparing them unresolved silently fails every inversion,
        # which empties the object list out of the commit body.
        try:
            relative = path.resolve().relative_to(self.database_root.resolve())
        except ValueError as exc:
            raise RepositoryError(f"{path} is not under {self.database_root}") from exc

        parts = relative.parts
        if len(parts) != 3:
            raise RepositoryError(f"{path} is not a <schema>/<type>/<name>.sql path")

        owner, folder, filename = parts
        if not filename.endswith(_EXTENSION):
            raise RepositoryError(f"{path} does not end in {_EXTENSION}")
        object_type = TYPE_BY_FOLDER.get(folder)
        if object_type is None:
            raise RepositoryError(f"{path} is in unknown type folder {folder!r}")

        return OracleObject(
            owner=owner,
            object_type=object_type,
            object_name=filename[: -len(_EXTENSION)],
            database=self.database,
        )

    def map_all(self, objects: Iterable[OracleObject]) -> dict[Path, OracleObject]:
        """Map many objects at once, rejecting case-insensitive collisions.

        Oracle can hold ``"Foo"`` and ``"FOO"`` as distinct objects, but a
        case-insensitive filesystem cannot hold both files. Left undetected,
        one would silently overwrite the other and reconciliation would then
        delete a live object's file.

        Args:
            objects: The objects to map.

        Returns:
            Path to object, for every object that mapped successfully.

        Raises:
            RepositoryError: If two objects map to paths differing only by case.
        """
        mapping: dict[Path, OracleObject] = {}
        seen: dict[str, str] = {}

        for obj in objects:
            path = self.to_path(obj)
            # Compare the *strings*, not the Paths: on Windows, Path equality
            # and hashing are already case-insensitive, so comparing Paths
            # would make this check a no-op on the one platform where a
            # case-insensitive collision actually breaks the checkout.
            text = str(path)
            key = text.casefold()
            if key in seen and seen[key] != text:
                raise RepositoryError(
                    f"{text} collides with {seen[key]} on a case-insensitive filesystem"
                )
            seen[key] = text
            mapping[path] = obj
        return mapping
