"""Oracle object value objects (docs/architecture.md §2, §4.3).

Pure data. Frozen dataclasses so a discovered object cannot be mutated as it
travels through extraction, normalization, and writing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
    "ExtractedSource",
    "ObjectStatus",
    "ObjectType",
    "OracleObject",
    "OracleStatus",
]


class ObjectType(StrEnum):
    """The twelve supported Oracle object types (docs/requirements.md §5).

    Values are the exact ``DBA_OBJECTS.OBJECT_TYPE`` strings, so they can be
    bound directly into the discovery query.
    """

    PACKAGE = "PACKAGE"
    PACKAGE_BODY = "PACKAGE BODY"
    PROCEDURE = "PROCEDURE"
    FUNCTION = "FUNCTION"
    TRIGGER = "TRIGGER"
    TYPE = "TYPE"
    TYPE_BODY = "TYPE BODY"
    VIEW = "VIEW"
    TABLE = "TABLE"
    SEQUENCE = "SEQUENCE"
    SYNONYM = "SYNONYM"
    MATERIALIZED_VIEW = "MATERIALIZED VIEW"


class OracleStatus(StrEnum):
    """``DBA_OBJECTS.STATUS``. Only these two are ever exported."""

    VALID = "VALID"
    INVALID = "INVALID"


class ObjectStatus(StrEnum):
    """Per-object processing outcome (docs/requirements.md §12)."""

    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    FAILED_EXTRACTION = "FAILED_EXTRACTION"
    FAILED_NORMALIZATION = "FAILED_NORMALIZATION"
    FAILED_COMPARISON = "FAILED_COMPARISON"
    FAILED_GIT = "FAILED_GIT"
    FAILED_PUSH = "FAILED_PUSH"
    FAILED_DATABASE = "FAILED_DATABASE"


@dataclass(frozen=True, slots=True, order=True)
class OracleObject:
    """One object discovered in ``DBA_OBJECTS``.

    Field order matches the mandated sort order (``OWNER, OBJECT_TYPE,
    OBJECT_NAME``) so that ``sorted()`` reproduces it and commit bodies are
    deterministic (docs/architecture.md §4.1, §11.5).
    """

    owner: str
    object_type: ObjectType
    object_name: str
    database: str
    status: OracleStatus = OracleStatus.VALID
    created: datetime | None = None
    last_ddl_time: datetime | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        """Deduplication key for this object: ``(owner, type, name)``."""
        return (self.owner, str(self.object_type), self.object_name)


@dataclass(frozen=True, slots=True)
class ExtractedSource:
    """Raw source text extracted for one object, before normalization."""

    obj: OracleObject
    text: str
