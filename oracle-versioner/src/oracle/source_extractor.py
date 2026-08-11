"""Dictionary-view extraction strategies (docs/architecture.md §5.2, §5.4).

Covers ``DBA_SOURCE`` (the seven PL/SQL types), ``DBA_VIEWS`` (views),
``DBA_TAB_COLUMNS`` (tables), and ``DBA_SYNONYMS`` (synonyms).

``DBA_SOURCE`` rows are fetched ``ORDER BY LINE`` and joined in retrieved
order. Interior line content is never trimmed, reflowed, or reordered here —
that is the normalizer's job.

**Views do not live in ``DBA_SOURCE``.** docs/requirements.md §6 maps VIEW to
``DBA_SOURCE``, but Oracle stores view text in ``DBA_VIEWS.TEXT``; querying
``DBA_SOURCE`` for a view returns zero rows, which would make every view look
dropped. :class:`ViewStrategy` uses ``DBA_VIEWS``, whose ``SELECT`` grant
architecture §21.2 already requires. See the Phase 3 report.
"""

from __future__ import annotations

from src.models.oracle_object import ExtractedSource, OracleObject
from src.oracle import sql
from src.oracle.connection import Session

__all__ = ["ColumnStrategy", "DictionaryStrategy", "SourceStrategy", "ViewStrategy"]


def _quoted(owner: str, name: str) -> str:
    """Render a schema-qualified, quoted identifier."""
    return f'"{owner}"."{name}"'


class SourceStrategy:
    """Line-ordered extraction from ``DBA_SOURCE``."""

    def __init__(self, session: Session) -> None:
        """Bind the strategy to a session."""
        self.session = session

    def extract(self, obj: OracleObject) -> ExtractedSource:
        """Extract ``obj``'s source, preserving line order exactly.

        Args:
            obj: The object to extract.

        Returns:
            The raw source prefixed with ``CREATE OR REPLACE``, because
            ``DBA_SOURCE`` line 1 begins at the object keyword. An empty result
            yields empty text, which the dispatcher reads as "object vanished".
        """
        rows = self.session.fetch_all(
            sql.SELECT_SOURCE,
            {"owner": obj.owner, "name": obj.object_name, "object_type": str(obj.object_type)},
        )
        if not rows:
            return ExtractedSource(obj=obj, text="")

        body = "".join(row[0] or "" for row in rows)
        if not body.strip():
            # Rows exist but carry no source. That is corruption, not a drop,
            # so return the blank text and let validation fail the object
            # rather than fabricating a bare CREATE OR REPLACE prefix.
            return ExtractedSource(obj=obj, text=body)
        return ExtractedSource(obj=obj, text="CREATE OR REPLACE " + body.lstrip())


class ViewStrategy:
    """``CREATE OR REPLACE VIEW`` rendering from ``DBA_VIEWS``."""

    def __init__(self, session: Session) -> None:
        """Bind the strategy to a session."""
        self.session = session

    def extract(self, obj: OracleObject) -> ExtractedSource:
        """Render ``obj`` as a view definition.

        Args:
            obj: The view to render.

        Returns:
            The rendered statement, or empty text when the view no longer exists.
        """
        text = self.session.fetch_value(
            sql.SELECT_VIEW_TEXT, {"owner": obj.owner, "name": obj.object_name}
        )
        if not text:
            return ExtractedSource(obj=obj, text="")

        header = f"CREATE OR REPLACE VIEW {_quoted(obj.owner, obj.object_name)} AS\n"
        return ExtractedSource(obj=obj, text=header + str(text).strip() + "\n")


class ColumnStrategy:
    """``CREATE TABLE`` rendering from ``DBA_TAB_COLUMNS``, ordered by ``COLUMN_ID``.

    This renders the column list only, as docs/requirements.md §6 specifies.
    Constraints, indexes, and partitioning are deliberately out of scope.
    """

    def __init__(self, session: Session) -> None:
        """Bind the strategy to a session."""
        self.session = session

    def extract(self, obj: OracleObject) -> ExtractedSource:
        """Render ``obj`` as a ``CREATE TABLE`` statement.

        Args:
            obj: The table to render.

        Returns:
            The rendered statement, or empty text when the table no longer exists.
        """
        rows = self.session.fetch_all(
            sql.SELECT_TABLE_COLUMNS, {"owner": obj.owner, "name": obj.object_name}
        )
        if not rows:
            return ExtractedSource(obj=obj, text="")

        columns = [self._column(row) for row in rows]
        body = ",\n".join(f"  {column}" for column in columns)
        text = f"CREATE TABLE {_quoted(obj.owner, obj.object_name)} (\n{body}\n);\n"
        return ExtractedSource(obj=obj, text=text)

    @staticmethod
    def _column(row: tuple) -> str:
        """Render one column definition from a ``DBA_TAB_COLUMNS`` row."""
        name, data_type, length, precision, scale, char_length, nullable, default = row

        rendered = str(data_type)
        if data_type in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"):
            rendered = f"{data_type}({char_length or length})"
        elif data_type == "NUMBER" and precision is not None:
            rendered = f"NUMBER({precision},{scale})" if scale else f"NUMBER({precision})"

        parts = [f'"{name}"', rendered]
        if default is not None and str(default).strip():
            parts.append(f"DEFAULT {str(default).strip()}")
        if nullable == "N":
            parts.append("NOT NULL")
        return " ".join(parts)


class DictionaryStrategy:
    """``CREATE OR REPLACE [PUBLIC] SYNONYM`` rendering from ``DBA_SYNONYMS``."""

    def __init__(self, session: Session) -> None:
        """Bind the strategy to a session."""
        self.session = session

    def extract(self, obj: OracleObject) -> ExtractedSource:
        """Render ``obj`` as a synonym statement.

        Args:
            obj: The synonym to render.

        Returns:
            The rendered statement, or empty text when the synonym is gone.
        """
        rows = self.session.fetch_all(
            sql.SELECT_SYNONYM, {"owner": obj.owner, "name": obj.object_name}
        )
        if not rows:
            return ExtractedSource(obj=obj, text="")

        table_owner, table_name, db_link = rows[0]
        scope = "PUBLIC " if obj.owner == "PUBLIC" else ""
        name = obj.object_name if scope else _quoted(obj.owner, obj.object_name)
        target = _quoted(table_owner, table_name) if table_owner else f'"{table_name}"'
        if db_link:
            target = f"{target}@{db_link}"
        return ExtractedSource(
            obj=obj, text=f"CREATE OR REPLACE {scope}SYNONYM {name} FOR {target};\n"
        )
