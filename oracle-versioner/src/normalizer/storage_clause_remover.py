"""Removal of storage / physical / environment clauses.

Second line of defence behind the ``DBMS_METADATA`` transform parameters
(docs/architecture.md §5.3), and the only defence for output that does not come
from ``DBMS_METADATA`` at all.

Two properties make this safe enough to run unattended:

* It never sees a string literal or a comment. ``-- keep the CACHE warm`` and
  ``'TABLESPACE users'`` survive untouched, because
  :func:`~src.normalizer.plsql_normalizer.transform_unprotected` hands this
  module only the unprotected spans.
* ``STORAGE(...)`` and ``LOB (...) STORE AS (...)`` are removed as **balanced
  parenthesis units**, not by line- or pattern-matching, so a nested
  ``BUFFER_POOL`` cannot leave a dangling ``)`` behind.

Word boundaries are built from Oracle's identifier alphabet, which includes
``$`` and ``#``: a column named ``CACHE_SIZE`` or ``LOGGING$`` is never touched.
"""

from __future__ import annotations

import re

from src.normalizer.plsql_normalizer import transform_unprotected

__all__ = ["STORAGE_CLAUSE_KEYWORDS", "remove_storage_clauses"]

#: The clauses listed in docs/requirements.md §7, plus the negated spellings of
#: the same attributes — removing ``LOGGING`` but keeping ``NOLOGGING`` would
#: make the output depend on which environment it was extracted from.
STORAGE_CLAUSE_KEYWORDS: tuple[str, ...] = (
    "STORAGE",
    "TABLESPACE",
    "PCTFREE",
    "PCTUSED",
    "INITRANS",
    "MAXTRANS",
    "SEGMENT CREATION",
    "LOGGING",
    "NOLOGGING",
    "COMPRESS",
    "NOCOMPRESS",
    "CACHE",
    "NOCACHE",
    "PARALLEL",
    "NOPARALLEL",
    "BUFFER_POOL",
    "FLASH_CACHE",
    "CELL_FLASH_CACHE",
)

# Oracle identifier characters, so CACHE_SIZE and MY$LOGGING are not matched.
_L = r"(?<![\w$#])"
_R = r"(?![\w$#])"
_NAME = r'(?:"[^"]+"|[A-Za-z][\w$#]*)'
_SIZE = r"\d+\s*[KMGT]?"

#: Clauses removed whole, in order. Each pattern is anchored on both sides by
#: an identifier boundary and consumes the clause's own argument.
#:
#: The leading ``[ \t]*`` matters: it swallows the space *before* the clause, so
#: removal cannot leave a double space behind. Nothing here may collapse
#: whitespace globally — that would flatten every line's indentation. Newlines
#: are deliberately not consumed; the blank lines left behind are what step 8
#: of the pipeline exists to clean up.
_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"[ \t]*{pattern}", re.IGNORECASE)
    for pattern in (
        rf"{_L}SEGMENT\s+CREATION\s+(?:IMMEDIATE|DEFERRED){_R}",
        rf"{_L}TABLESPACE\s+{_NAME}",
        rf"{_L}(?:PCTFREE|PCTUSED|INITRANS|MAXTRANS|PCTINCREASE|FREELISTS)\s+\d+{_R}",
        rf"{_L}FREELIST\s+GROUPS\s+\d+{_R}",
        rf"{_L}(?:INITIAL|NEXT)\s+{_SIZE}{_R}",
        rf"{_L}MINEXTENTS\s+\d+{_R}",
        rf"{_L}MAXEXTENTS\s+(?:\d+|UNLIMITED){_R}",
        rf"{_L}(?:BUFFER_POOL|FLASH_CACHE|CELL_FLASH_CACHE)\s+{_NAME}",
        rf"{_L}(?:NO)?PARALLEL(?:\s+\d+)?{_R}",
        # CACHE takes a size on a sequence and none on a table; both go.
        rf"{_L}(?:NO)?CACHE(?:\s+\d+)?{_R}",
        rf"{_L}(?:NO)?(?:LOGGING|COMPRESS|MONITORING){_R}",
    )
)

_STORAGE_OPEN = re.compile(rf"{_L}STORAGE\s*\(", re.IGNORECASE)
_LOB_OPEN = re.compile(rf"{_L}LOB\s*\(\s*{_NAME}\s*\)\s*STORE\s+AS\s*(?:{_NAME}\s*)?\(", re.IGNORECASE)

# A quoted identifier can legitimately BE a clause word — a column named
# "cache" is perfectly legal. Such identifiers are masked behind a placeholder
# before the patterns run, so a clause keyword is only ever matched as a
# keyword. The placeholder is still shaped like a name, so `TABLESPACE "USERS"`
# continues to match and be removed as a unit.
_QUOTED_IDENTIFIER = re.compile(r'"[^"]*"')
_MASK_PREFIX = "Q$I$"


def _mask_quoted(text: str) -> tuple[str, list[str]]:
    """Replace quoted identifiers with name-shaped placeholders."""
    tokens: list[str] = []

    def capture(match: re.Match[str]) -> str:
        tokens.append(match.group(0))
        return f"{_MASK_PREFIX}{len(tokens) - 1}$"

    return _QUOTED_IDENTIFIER.sub(capture, text), tokens


def _unmask_quoted(text: str, tokens: list[str]) -> str:
    """Restore the identifiers masked by :func:`_mask_quoted`."""
    for index, token in enumerate(tokens):
        text = text.replace(f"{_MASK_PREFIX}{index}$", token)
    return text


def _remove_balanced(text: str, opener: re.Pattern[str]) -> str:
    """Remove every ``<keyword>( ... )`` occurrence as a balanced unit.

    Args:
        text: Unprotected text — no literals or comments.
        opener: A pattern whose match ends at the opening parenthesis.

    Returns:
        The text with each matched clause and its balanced body removed.
    """
    while True:
        match = opener.search(text)
        if match is None:
            return text

        start = match.start()
        while start and text[start - 1] in " \t":
            start -= 1

        depth = 1
        index = match.end()
        while index < len(text) and depth:
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
            index += 1
        if depth:
            # Unbalanced input: leave it alone rather than truncate the object.
            return text
        text = text[:start] + text[index:]


def remove_storage_clauses(source: str) -> str:
    """Strip environment-specific storage clauses from ``source``.

    Args:
        source: The text to transform.

    Returns:
        The transformed text, with balanced clause bodies removed and literals
        and comments untouched.
    """

    def strip(text: str) -> str:
        text, tokens = _mask_quoted(text)
        text = _remove_balanced(text, _LOB_OPEN)
        text = _remove_balanced(text, _STORAGE_OPEN)
        for pattern in _PATTERNS:
            text = pattern.sub("", text)
        return _unmask_quoted(text, tokens)

    return transform_unprotected(source, strip)
