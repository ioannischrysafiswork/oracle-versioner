"""Literal- and comment-aware text handling (docs/architecture.md §6.2).

The dangerous normalization steps are storage-clause removal, quoted-identifier
normalization, and keyword casing. All three would corrupt code if applied
naively, so none of them ever sees raw text: a lexical scanner first splits the
source into string literals, ``--`` comments, ``/* */`` comments, quoted
identifiers, and code, and the transforms run only on the regions where they
are safe.

That is what makes a package containing the word ``CACHE`` in a comment, or
``'STORAGE('`` in a literal, survive untouched.

Two different notions of "safe" are needed, and conflating them is a bug:

* **Code only** — keyword casing. A quoted identifier's case is significant, so
  ``"myTable"`` must never be re-cased.
* **Everything except literals and comments** — storage-clause removal. A
  clause like ``TABLESPACE "USERS"`` spans a code region *and* a quoted
  identifier, so those spans are merged before the transform sees them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import StrEnum

__all__ = [
    "KEYWORDS",
    "RESERVED_WORDS",
    "Region",
    "normalize_keyword_case",
    "normalize_quoted_identifiers",
    "scan_regions",
    "string_literal_spans",
    "transform_code",
    "transform_unprotected",
]

#: Oracle identifier characters. ``$`` and ``#`` are legal, which is why the
#: plain ``\b`` word boundary is not sufficient anywhere in this package.
_IDENT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$#")

#: Alternative delimiter pairs for ``q'[...]'`` style literals.
_Q_PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">"}


class Region(StrEnum):
    """The region kinds the lexical scanner distinguishes."""

    CODE = "CODE"
    STRING_LITERAL = "STRING_LITERAL"
    LINE_COMMENT = "LINE_COMMENT"
    BLOCK_COMMENT = "BLOCK_COMMENT"
    QUOTED_IDENTIFIER = "QUOTED_IDENTIFIER"


#: Keywords that are upper-cased, in code regions only. Deliberately limited to
#: reserved words: those cannot be used as unquoted identifiers, so re-casing
#: one can never touch a name.
KEYWORDS: frozenset[str] = frozenset(
    """
    ALL ALTER AND ANY AS ASC BEGIN BETWEEN BY CASE CHAR CHECK CLOSE CLUSTER
    COLUMN COMMENT COMPRESS CONNECT CONSTANT CREATE CURRENT CURSOR DATE
    DECIMAL DECLARE DEFAULT DELETE DESC DISTINCT DROP ELSE ELSIF END EXCEPTION
    EXCLUSIVE EXISTS EXIT FETCH FLOAT FOR FROM FUNCTION GOTO GRANT GROUP
    HAVING IDENTIFIED IF IN INDEX INSERT INTEGER INTERSECT INTO IS LIKE LOCK
    LONG LOOP MINUS MODE NOCOMPRESS NOT NOWAIT NULL NUMBER OF ON OPTION OR
    ORDER OVERLAPS PACKAGE PRAGMA PRIOR PROCEDURE PUBLIC RAISE RAW RENAME
    RESOURCE RETURN REVOKE ROLLBACK ROWID SELECT SET SHARE SIZE SMALLINT START
    THEN TO TRIGGER UNION UNIQUE UPDATE VALUES VARCHAR VARCHAR2 VIEW WHEN
    WHERE WHILE WITH
    """.split()
)

#: Words that must stay quoted when they appear as a quoted identifier.
#:
#: Two reasons a word belongs here. The obvious one: unquoting would turn a
#: name into a keyword. The subtle one: a column may legally be named
#: ``"CACHE"``, and unquoting it to a bare ``CACHE`` would expose it to the
#: storage-clause remover on the *next* run, which would delete it. That makes
#: normalization non-idempotent and silently corrupts the object, so every
#: storage-clause keyword is listed here too — see the test asserting that
#: ``STORAGE_CLAUSE_KEYWORDS`` stays a subset of this set.
RESERVED_WORDS: frozenset[str] = KEYWORDS | frozenset(
    """
    ACCESS AUDIT BODY DATE DECIMAL ELSIF EXIT FILE FLOAT GOTO IMMEDIATE
    INCREMENT INITIAL INTEGER LEVEL LOOP LONG MAXEXTENTS MODIFY NOAUDIT
    NUMBER OFFLINE ONLINE PCTFREE PRAGMA RAW ROW ROWID ROWNUM ROWS SESSION
    SMALLINT SUCCESSFUL SYNONYM SYSDATE TABLE THEN TYPE UID USER VALIDATE
    VARCHAR VARCHAR2 WHENEVER WHILE

    BUFFER_POOL CACHE CELL_FLASH_CACHE COMPRESS CREATION DEFERRED FLASH_CACHE
    FREELIST FREELISTS GROUPS LOGGING MINEXTENTS MONITORING NEXT NOCACHE
    NOCOMPRESS NOLOGGING NOMONITORING NOPARALLEL PARALLEL PCTINCREASE PCTUSED
    INITRANS MAXTRANS SEGMENT STORAGE TABLESPACE
    """.split()
)

#: A quoted identifier is only unquoted when it is already in Oracle's default
#: upper-case form; anything else changes meaning when the quotes are removed.
_PLAIN_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_$#]*$")

_WORD = re.compile(r"(?<![\w$#])[A-Za-z][A-Za-z0-9_$#]*(?![\w$#])")


def _scan_delimited(source: str, start: int, quote: str) -> int:
    """Return the index just past a ``'``- or ``"``-delimited token.

    A doubled quote is an escape, not a terminator. An unterminated token runs
    to the end of the input rather than raising: normalization must never fail
    on input the database itself accepted.
    """
    index = start + 1
    length = len(source)
    while index < length:
        if source[index] == quote:
            if index + 1 < length and source[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _scan_q_literal(source: str, start: int) -> int | None:
    """Return the index just past a ``q'...'`` literal, or ``None``.

    Handles the ``nq'`` prefix and the ``()``, ``[]``, ``{}``, ``<>``
    alternative delimiters.
    """
    index = start
    if source[index] in "nN":
        index += 1
    if index >= len(source) or source[index] not in "qQ":
        return None
    if index + 1 >= len(source) or source[index + 1] != "'":
        return None

    opening = source[index + 2] if index + 2 < len(source) else ""
    if not opening:
        return None
    closing = _Q_PAIRS.get(opening, opening)
    end = source.find(closing + "'", index + 3)
    return len(source) if end == -1 else end + 2


def scan_regions(source: str) -> list[tuple[Region, int, int]]:
    """Split ``source`` into typed, non-overlapping regions.

    Args:
        source: The text to scan.

    Returns:
        ``(kind, start, end)`` triples covering the whole input, in order.
    """
    regions: list[tuple[Region, int, int]] = []
    length = len(source)
    index = 0
    code_start = 0

    while index < length:
        char = source[index]
        at_boundary = index == 0 or source[index - 1] not in _IDENT
        start = index

        if char == "'":
            kind, end = Region.STRING_LITERAL, _scan_delimited(source, index, "'")
        elif char == '"':
            kind, end = Region.QUOTED_IDENTIFIER, _scan_delimited(source, index, '"')
        elif at_boundary and char in "qQnN" and (q_end := _scan_q_literal(source, index)):
            kind, end = Region.STRING_LITERAL, q_end
        elif source.startswith("--", index):
            found = source.find("\n", index)
            kind, end = Region.LINE_COMMENT, (length if found == -1 else found)
        elif source.startswith("/*", index):
            found = source.find("*/", index + 2)
            kind, end = Region.BLOCK_COMMENT, (length if found == -1 else found + 2)
        else:
            index += 1
            continue

        if start > code_start:
            regions.append((Region.CODE, code_start, start))
        regions.append((kind, start, end))
        index = code_start = end

    if code_start < length:
        regions.append((Region.CODE, code_start, length))
    return regions


def string_literal_spans(source: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` spans of every string literal.

    Used by the line-oriented pipeline steps, which must not alter whitespace
    inside a multi-line literal.
    """
    return [
        (start, end)
        for kind, start, end in scan_regions(source)
        if kind is Region.STRING_LITERAL
    ]


def _rebuild(source: str, kinds: set[Region], func: Callable[[str], str]) -> str:
    """Apply ``func`` to contiguous runs of the given region kinds.

    Runs are merged before ``func`` sees them, so a construct spanning a code
    region and a quoted identifier stays contiguous.
    """
    output: list[str] = []
    buffer: list[str] = []

    for kind, start, end in scan_regions(source):
        if kind in kinds:
            buffer.append(source[start:end])
            continue
        if buffer:
            output.append(func("".join(buffer)))
            buffer = []
        output.append(source[start:end])

    if buffer:
        output.append(func("".join(buffer)))
    return "".join(output)


def transform_code(source: str, func: Callable[[str], str]) -> str:
    """Apply ``func`` to code regions only, leaving quoted identifiers alone."""
    return _rebuild(source, {Region.CODE}, func)


def transform_unprotected(source: str, func: Callable[[str], str]) -> str:
    """Apply ``func`` everywhere except string literals and comments."""
    return _rebuild(source, {Region.CODE, Region.QUOTED_IDENTIFIER}, func)


def normalize_keyword_case(source: str) -> str:
    """Upper-case reserved words, in code regions only.

    Args:
        source: The text to transform.

    Returns:
        The transformed text. Quoted identifiers, literals, and comments are
        untouched, and only words in :data:`KEYWORDS` are altered.
    """

    def upper(text: str) -> str:
        return _WORD.sub(
            lambda m: m.group(0).upper() if m.group(0).upper() in KEYWORDS else m.group(0),
            text,
        )

    return transform_code(source, upper)


def normalize_quoted_identifiers(source: str) -> str:
    """Remove quotes from identifiers that are safely unquotable.

    An identifier is only unquoted when it is already in Oracle's default
    upper-case form and is not a reserved word — then the quotes are pure
    noise. ``"myTable"``, ``"WITH SPACE"``, and ``"SELECT"`` all keep theirs,
    because removing them would change what the statement means.

    Args:
        source: The text to transform.

    Returns:
        The transformed text.
    """
    output: list[str] = []
    for kind, start, end in scan_regions(source):
        chunk = source[start:end]
        if kind is Region.QUOTED_IDENTIFIER and len(chunk) > 2:
            inner = chunk[1:-1]
            if '""' not in inner and _PLAIN_IDENTIFIER.match(inner) and inner not in RESERVED_WORDS:
                chunk = inner
        output.append(chunk)
    return "".join(output)
