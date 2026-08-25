"""The normalization pipeline (docs/architecture.md §6.1).

A fixed, ordered chain of pure functions. **The order is part of the contract**
— determinism depends on it, and steps 8-10 run last so earlier removals cannot
leave residual blank lines or trailing whitespace behind:

    1.  unify line endings to LF
    2.  expand tabs to spaces
    3.  strip trailing whitespace per line
    4.  remove storage / physical / environment clauses
    5.  normalize CREATE OR REPLACE header formatting
    6.  normalize quoted identifiers (only when safely unquotable)
    7.  normalize keyword casing (literal- and comment-aware)
    8.  collapse repeated blank lines
    9.  normalize statement terminator
    10. ensure exactly one trailing newline

Guarantees enforced by tests: ``normalize(normalize(x)) == normalize(x)``, and
the token stream of code regions is unchanged (logic preservation).

The line-oriented steps (2, 3, 8) skip text inside string literals. A tab or a
run of trailing spaces inside a multi-line literal is *data*: rewriting it
would change what the program does, which normalization must never do.

Nothing here depends on dict iteration order, locale, timezone, environment, or
the filesystem, and no timestamp, hostname, or run identifier is ever embedded
in the output.
"""

from __future__ import annotations

import re

from src.config.models import NormalizationSettings
from src.exceptions import NormalizationError
from src.models.oracle_object import ObjectType
from src.normalizer.plsql_normalizer import (
    normalize_keyword_case,
    normalize_quoted_identifiers,
    string_literal_spans,
)
from src.normalizer.storage_clause_remover import remove_storage_clauses

__all__ = ["PIPELINE_STEPS", "PLSQL_TERMINATED_TYPES", "DdlNormalizer"]

#: The ordered pipeline, for documentation and for logging the active rule set.
PIPELINE_STEPS: tuple[str, ...] = (
    "unify_line_endings",
    "expand_tabs",
    "strip_trailing_whitespace",
    "remove_storage_clauses",
    "normalize_create_or_replace",
    "normalize_quoted_identifiers",
    "normalize_keyword_case",
    "collapse_blank_lines",
    "normalize_terminator",
    "ensure_trailing_newline",
)

#: Types whose stored source is a PL/SQL block, and therefore terminates with a
#: lone ``/`` rather than a semicolon. A package body ends in ``END;``, which
#: SQL*Plus will not execute without the slash.
PLSQL_TERMINATED_TYPES: frozenset[ObjectType] = frozenset(
    {
        ObjectType.PACKAGE,
        ObjectType.PACKAGE_BODY,
        ObjectType.PROCEDURE,
        ObjectType.FUNCTION,
        ObjectType.TYPE,
        ObjectType.TYPE_BODY,
        ObjectType.TRIGGER,
    }
)

_HEADER = re.compile(
    r"^\s*CREATE(\s+OR\s+REPLACE)?(\s+(?:NON)?EDITIONABLE)?\s+"
    r"(PACKAGE\s+BODY|TYPE\s+BODY|MATERIALIZED\s+VIEW|"
    r"PACKAGE|PROCEDURE|FUNCTION|TRIGGER|TYPE|VIEW|TABLE|SEQUENCE|SYNONYM)",
    re.IGNORECASE,
)
# A PL/SQL block legitimately *ends* in `END;` — that semicolon is part of the
# code, not a terminator. Only the trailing slashes may be stripped from one,
# or the object is silently truncated.
_TRAILING_SLASHES = re.compile(r"(?:\s*/)+\s*$")
_TRAILING_SEMICOLONS = re.compile(r"(?:\s*[;/])+\s*$")


class DdlNormalizer:
    """Applies the normalization pipeline to extracted source."""

    def __init__(self, settings: NormalizationSettings | None = None) -> None:
        """Create a normalizer.

        Args:
            settings: Which rules are enabled. Defaults to all enabled.
        """
        self.settings = settings or NormalizationSettings()

    def normalize(self, source: str, object_type: ObjectType | None = None) -> str:
        """Run the full pipeline over ``source``.

        Args:
            source: Raw extracted source.
            object_type: Decides the statement terminator. When ``None`` the
                existing terminator is tidied but never changed.

        Returns:
            Deterministic, normalized source ending in exactly one newline.

        Raises:
            NormalizationError: If a rule fails. The object then keeps its
                existing repository file rather than being overwritten.
        """
        rules = self.settings
        try:
            text = source.replace("\r\n", "\n").replace("\r", "\n") if rules.unify_line_endings \
                else source
            if rules.expand_tabs:
                text = self._expand_tabs(text, rules.tab_width)
            if rules.strip_trailing_whitespace:
                text = self._strip_trailing(text)
            if rules.remove_storage_clauses:
                text = remove_storage_clauses(text)
            if rules.normalize_create_or_replace:
                text = self._normalize_header(text)
            if rules.normalize_quoted_identifiers:
                text = normalize_quoted_identifiers(text)
            if rules.normalize_keyword_case:
                text = normalize_keyword_case(text)
            if rules.collapse_blank_lines:
                text = self._collapse_blank_lines(text)
            if rules.normalize_terminator:
                text = self._normalize_terminator(text, object_type)
            if rules.ensure_trailing_newline:
                text = text.rstrip("\n") + "\n"
        except NormalizationError:
            raise
        except Exception as exc:
            raise NormalizationError(f"normalization failed: {exc}") from exc

        return text

    @staticmethod
    def _line_guard(text: str) -> list[tuple[int, int]]:
        """Return the string-literal spans that line-oriented steps must skip."""
        return string_literal_spans(text)

    @staticmethod
    def _inside(spans: list[tuple[int, int]], offset: int) -> bool:
        """Whether ``offset`` falls inside one of ``spans``."""
        return any(start <= offset < end for start, end in spans)

    @classmethod
    def _expand_tabs(cls, text: str, width: int) -> str:
        """Replace each tab with ``width`` spaces, except inside string literals.

        Deliberately a fixed-width substitution rather than ``str.expandtabs``:
        column-aware expansion would have to be abandoned inside literals, and
        a rule that changes behaviour depending on whether the object happens
        to contain a string is not one anybody can reason about.
        """
        if "\t" not in text:
            return text
        spans = cls._line_guard(text)
        spaces = " " * width
        return "".join(
            char if char != "\t" or cls._inside(spans, index) else spaces
            for index, char in enumerate(text)
        )

    @classmethod
    def _strip_trailing(cls, text: str) -> str:
        """Strip trailing whitespace per line, except inside string literals."""
        spans = cls._line_guard(text)
        output: list[str] = []
        offset = 0
        for line in text.split("\n"):
            stripped = line.rstrip(" \t")
            cut = len(line) - len(stripped)
            if cut and cls._inside(spans, offset + len(stripped)):
                output.append(line)
            else:
                output.append(stripped)
            offset += len(line) + 1
        return "\n".join(output)

    @classmethod
    def _collapse_blank_lines(cls, text: str) -> str:
        """Collapse runs of blank lines to one, except inside string literals."""
        spans = cls._line_guard(text)
        output: list[str] = []
        offset = 0
        previous_blank = False
        for line in text.split("\n"):
            blank = not line.strip()
            protected = cls._inside(spans, offset)
            if blank and previous_blank and not protected:
                offset += len(line) + 1
                continue
            output.append(line)
            previous_blank = blank and not protected
            offset += len(line) + 1
        return "\n".join(output)

    @staticmethod
    def _normalize_header(text: str) -> str:
        """Collapse whitespace in the leading ``CREATE OR REPLACE`` header.

        Only the header is rewritten, and only its whitespace and case — the
        object name that follows is left exactly as extracted.
        """
        match = _HEADER.match(text)
        if match is None:
            return text

        parts = ["CREATE"]
        if match.group(1):
            parts.append("OR REPLACE")
        if match.group(2):
            parts.append(" ".join(match.group(2).split()).upper())
        parts.append(" ".join(match.group(3).split()).upper())
        return " ".join(parts) + text[match.end():]

    @staticmethod
    def _normalize_terminator(text: str, object_type: ObjectType | None) -> str:
        """Reduce trailing terminators to the single correct one.

        Args:
            text: The text so far.
            object_type: Decides between ``/`` and ``;``. When ``None``, an
                existing terminator is normalized in place and none is added.

        Returns:
            The text with exactly one terminator.
        """
        plsql = object_type in PLSQL_TERMINATED_TYPES if object_type else "/" in text[-4:]

        if plsql:
            # Strip only the slashes; `END;` keeps its semicolon.
            return _TRAILING_SLASHES.sub("", text).rstrip() + "\n/"

        body = _TRAILING_SEMICOLONS.sub("", text).rstrip()
        if object_type is None and body == text.rstrip():
            return body
        return body + ";"
