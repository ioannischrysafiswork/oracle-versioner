"""``DBMS_METADATA`` extraction for SEQUENCE and MATERIALIZED VIEW.

See docs/architecture.md §5.3. Transform parameters are set once per session by
:meth:`src.oracle.connection.OracleSession.initialize` — a read-only,
session-scoped call — so storage noise is suppressed *in Oracle* rather than
stripped afterwards with regular expressions. The textual
:mod:`src.normalizer.storage_clause_remover` is the second line of defence.

The returned CLOB is read in full and never truncated.
"""

from __future__ import annotations

from src.models.oracle_object import ExtractedSource, OracleObject
from src.oracle import sql
from src.oracle.connection import Session

__all__ = ["TRANSFORM_PARAMS", "MetadataStrategy"]

#: Re-exported from :mod:`src.oracle.sql`, which owns the statement text.
TRANSFORM_PARAMS = sql.TRANSFORM_PARAMS


class MetadataStrategy:
    """``DBMS_METADATA.GET_DDL`` based extraction."""

    def __init__(self, session: Session) -> None:
        """Bind the strategy to a session."""
        self.session = session

    def extract(self, obj: OracleObject) -> ExtractedSource:
        """Extract ``obj``'s DDL through ``DBMS_METADATA``.

        Args:
            obj: The object to extract.

        Returns:
            The full DDL text, or empty text when the object no longer exists.

        Raises:
            KeyError: If the object type has no ``DBMS_METADATA`` spelling,
                which would be a dispatch-table bug rather than a data problem.
        """
        metadata_type = sql.METADATA_TYPE_NAMES[obj.object_type]
        text = self.session.fetch_value(
            sql.SELECT_METADATA_DDL,
            {"object_type": metadata_type, "name": obj.object_name, "owner": obj.owner},
        )
        if not text:
            return ExtractedSource(obj=obj, text="")
        return ExtractedSource(obj=obj, text=str(text).strip() + "\n")
