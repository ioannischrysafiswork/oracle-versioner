"""Normalization.

Four things are proved here, because normalization is the one place where a
bug silently corrupts customer code rather than failing loudly:

* **Golden files** per object type — input to expected output.
* **Idempotency** — ``normalize(normalize(x)) == normalize(x)`` over the whole
  corpus, which is what makes the repository stable across runs.
* **Literal and comment safety** — ``CACHE``, ``STORAGE``, and friends inside
  strings and comments must survive verbatim.
* **Logic preservation** — the token stream of code regions is unchanged, which
  is the closest thing to a proof that no PL/SQL was harmed.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import replace
from pathlib import Path

import pytest

from src.config.models import NormalizationSettings
from src.models.oracle_object import ObjectType
from src.normalizer.ddl_normalizer import (
    PIPELINE_STEPS,
    PLSQL_TERMINATED_TYPES,
    DdlNormalizer,
)
from src.normalizer.plsql_normalizer import (
    RESERVED_WORDS,
    Region,
    normalize_keyword_case,
    normalize_quoted_identifiers,
    scan_regions,
    transform_code,
)
from src.normalizer.storage_clause_remover import (
    STORAGE_CLAUSE_KEYWORDS,
    remove_storage_clauses,
)

FIXTURES = Path(__file__).parent / "fixtures" / "normalizer"

#: Golden file stem to the object type it is extracted for.
GOLDEN_TYPES = {
    "package_body": ObjectType.PACKAGE_BODY,
    "table": ObjectType.TABLE,
    "view": ObjectType.VIEW,
    "sequence": ObjectType.SEQUENCE,
}


def _normalize(text: str, object_type: ObjectType | None = None) -> str:
    return DdlNormalizer().normalize(text, object_type)


# --------------------------------------------------------------------------- contract


def test_pipeline_order_matches_the_architecture() -> None:
    """The step order is part of the contract (docs/architecture.md §6.1)."""
    assert PIPELINE_STEPS == (
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


def test_cleanup_steps_run_last() -> None:
    """Blank-line, terminator, and newline steps must follow every removal."""
    order = {step: index for index, step in enumerate(PIPELINE_STEPS)}
    assert order["collapse_blank_lines"] > order["remove_storage_clauses"]
    assert order["ensure_trailing_newline"] == len(PIPELINE_STEPS) - 1


def test_storage_keywords_are_never_unquoted() -> None:
    """Unquoting a clause word would expose it to removal on the next run.

    This subset relationship is what keeps normalization idempotent for a
    column legally named ``"CACHE"``; letting the two lists drift apart would
    silently start deleting such columns.
    """
    words = {word for clause in STORAGE_CLAUSE_KEYWORDS for word in clause.split()}
    assert words <= RESERVED_WORDS


def test_every_specified_storage_clause_is_covered() -> None:
    """Every clause in docs/requirements.md §7 is in the removal list."""
    required = {
        "STORAGE", "TABLESPACE", "PCTFREE", "PCTUSED", "INITRANS", "MAXTRANS",
        "SEGMENT CREATION", "LOGGING", "NOCOMPRESS", "CACHE", "NOPARALLEL",
        "BUFFER_POOL", "FLASH_CACHE", "CELL_FLASH_CACHE",
    }
    assert required <= set(STORAGE_CLAUSE_KEYWORDS)


# --------------------------------------------------------------------------- golden files


@pytest.mark.parametrize("stem", sorted(GOLDEN_TYPES))
def test_golden_file(stem: str) -> None:
    """Normalizing the input reproduces the committed expected output."""
    source = (FIXTURES / f"{stem}.in.sql").read_text(encoding="utf-8")
    expected = (FIXTURES / f"{stem}.out.sql").read_text(encoding="utf-8")
    assert _normalize(source, GOLDEN_TYPES[stem]) == expected


@pytest.mark.parametrize("stem", sorted(GOLDEN_TYPES))
def test_golden_output_is_already_normalized(stem: str) -> None:
    """The committed expected output is a fixed point of the pipeline."""
    expected = (FIXTURES / f"{stem}.out.sql").read_text(encoding="utf-8")
    assert _normalize(expected, GOLDEN_TYPES[stem]) == expected


# --------------------------------------------------------------------------- idempotency


CORPUS: tuple[str, ...] = (
    "",
    "\n\n\n",
    "CREATE OR REPLACE PACKAGE p AS END;",
    "create\tor\nreplace  package body   p as end;",
    "CREATE TABLE t (c NUMBER) TABLESPACE users STORAGE(INITIAL 64K);",
    "-- only a comment\n",
    "/* block */ SELECT 1 FROM dual;",
    "SELECT 'it''s quoted' FROM dual;",
    "SELECT q'{unterminated" ,
    'CREATE OR REPLACE VIEW "HR"."V" AS SELECT "Odd Name" FROM t;',
    "BEGIN NULL; END;\n/\n/\n",
    "SELECT 1 FROM dual;;;",
    # A column legally named "CACHE". If unquoting exposed it as a bare
    # keyword, the next run's storage remover would delete it.
    'SELECT t."CACHE", t."TABLESPACE" FROM t;',
    'CREATE TABLE t ("LOGGING" NUMBER) TABLESPACE "USERS";',
)


@pytest.mark.parametrize("source", CORPUS)
def test_normalization_is_idempotent(source: str) -> None:
    """Running normalization twice produces identical output."""
    once = _normalize(source)
    assert _normalize(once) == once


@pytest.mark.parametrize("stem", sorted(GOLDEN_TYPES))
def test_golden_inputs_are_idempotent(stem: str) -> None:
    """Idempotency holds for the realistic corpus too."""
    source = (FIXTURES / f"{stem}.in.sql").read_text(encoding="utf-8")
    object_type = GOLDEN_TYPES[stem]
    once = _normalize(source, object_type)
    assert _normalize(once, object_type) == once


@pytest.mark.parametrize("source", CORPUS)
def test_output_always_ends_in_exactly_one_newline(source: str) -> None:
    r"""No "\ No newline at end of file" churn, ever."""
    result = _normalize(source)
    assert result.endswith("\n")
    assert not result.endswith("\n\n")


@pytest.mark.parametrize("source", CORPUS)
def test_output_never_contains_crlf(source: str) -> None:
    """LF only; a CRLF would be a phantom diff on Windows checkouts."""
    assert "\r" not in _normalize(source.replace("\n", "\r\n"))


_FUZZ_FRAGMENTS = (
    "CREATE OR REPLACE PACKAGE BODY p AS", "END;", "BEGIN", "NULL;",
    "v := 'CACHE TABLESPACE x';", "-- STORAGE(INITIAL 1M)", "/* NOLOGGING */",
    't."CACHE"', 't."Odd Name"', "TABLESPACE users", "STORAGE(INITIAL 64K)",
    "PCTFREE 10", "q'[NOPARALLEL]'", "NOCOMPRESS", "\t", "   ", "", "CACHE 20",
    'LOB ("C") STORE AS (TABLESPACE u)', "SELECT 1 FROM dual", '"SELECT"',
)


def test_fuzzed_combinations_stay_idempotent_and_balanced() -> None:
    """Randomized fragment soup, on a fixed seed so failures are reproducible.

    The hand-written corpus above only covers cases somebody thought of; this
    covers the interactions between rules, which is where the damage hides.
    """
    import random

    rng = random.Random(20260810)  # noqa: S311 - reproducible fuzzing, not crypto
    types: list[ObjectType | None] = [None, *ObjectType]

    for _ in range(1500):
        source = "\n".join(
            rng.choice(_FUZZ_FRAGMENTS) for _ in range(rng.randint(1, 6))
        )
        object_type = rng.choice(types)
        once = _normalize(source, object_type)

        assert _normalize(once, object_type) == once, f"not idempotent: {source!r}"
        if source.count("(") == source.count(")"):
            assert once.count("(") == once.count(")"), f"unbalanced: {source!r}"
        if "'CACHE TABLESPACE x'" in source:
            assert "'CACHE TABLESPACE x'" in once, f"literal lost: {source!r}"


# --------------------------------------------------------------------------- the scanner


def test_scanner_distinguishes_literals_and_comments() -> None:
    """The region kinds that make steps 4, 6, and 7 safe are all declared."""
    assert {
        Region.CODE, Region.STRING_LITERAL, Region.LINE_COMMENT,
        Region.BLOCK_COMMENT, Region.QUOTED_IDENTIFIER,
    } == set(Region)


def test_regions_cover_the_whole_input_without_gaps() -> None:
    """A gap would silently drop characters from the output."""
    source = "SELECT 'a' -- c\n/* b */ \"Q\" FROM dual"
    regions = scan_regions(source)
    assert regions[0][1] == 0
    assert regions[-1][2] == len(source)
    for (_, _, end), (_, start, _) in itertools.pairwise(regions):
        assert end == start


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("'plain'", Region.STRING_LITERAL),
        ("'it''s'", Region.STRING_LITERAL),
        ("q'[body]'", Region.STRING_LITERAL),
        ("q'(body)'", Region.STRING_LITERAL),
        ("q'!body!'", Region.STRING_LITERAL),
        ("nq'{body}'", Region.STRING_LITERAL),
        ("N'body'", Region.CODE),
        ('"Quoted"', Region.QUOTED_IDENTIFIER),
        ("-- comment", Region.LINE_COMMENT),
        ("/* comment */", Region.BLOCK_COMMENT),
    ],
)
def test_scanner_classifies_tokens(source: str, expected: Region) -> None:
    """Every Oracle literal form is recognized, including q-quoting."""
    assert scan_regions(source)[0][0] is expected


def test_q_prefix_inside_an_identifier_is_not_a_literal() -> None:
    """`aliasq'...'` is code followed by a literal, not a q-quoted string."""
    kinds = [kind for kind, _, _ in scan_regions("myq'x'")]
    assert kinds[0] is Region.CODE


def test_unterminated_literal_does_not_raise() -> None:
    """Normalization must never fail on input the database itself accepted."""
    assert scan_regions("SELECT 'oops")[-1][0] is Region.STRING_LITERAL


# --------------------------------------------------------------------------- literal safety


LITERAL_SAFETY = (
    "SELECT 'STORAGE(INITIAL 64K)' FROM dual;",
    "SELECT 'TABLESPACE users' FROM dual;",
    "SELECT 'NOLOGGING CACHE PCTFREE 10' FROM dual;",
    "-- CACHE and STORAGE and TABLESPACE users\nSELECT 1 FROM dual;",
    "/* PCTFREE 10 NOCOMPRESS LOGGING */\nSELECT 1 FROM dual;",
    "SELECT q'[TABLESPACE users STORAGE(INITIAL 64K)]' FROM dual;",
)


@pytest.mark.parametrize("source", LITERAL_SAFETY)
def test_storage_keywords_survive_inside_literals_and_comments(source: str) -> None:
    """A package mentioning CACHE in a string or comment is never touched."""
    protected = re.findall(r"'[^']*'|--[^\n]*|/\*.*?\*/", source, re.DOTALL)
    result = remove_storage_clauses(source)
    for fragment in protected:
        assert fragment in result


@pytest.mark.parametrize("source", LITERAL_SAFETY)
def test_full_pipeline_preserves_literals_and_comments(source: str) -> None:
    """The guarantee holds through the whole pipeline, not just one step."""
    protected = re.findall(r"'[^']*'|--[^\n]*|/\*.*?\*/", source, re.DOTALL)
    result = _normalize(source)
    for fragment in protected:
        assert fragment in result


def test_keyword_casing_does_not_touch_comments_or_literals() -> None:
    """Lower-case keywords inside a comment or a string stay lower-case."""
    source = "-- select from dual\nselect 'select from dual' from dual"
    result = normalize_keyword_case(source)
    assert "-- select from dual" in result
    assert "'select from dual'" in result
    assert "SELECT 'select from dual' FROM dual" in result


def test_keyword_casing_does_not_touch_quoted_identifiers() -> None:
    """A quoted identifier's case is significant and must be preserved."""
    assert normalize_keyword_case('SELECT "select" FROM t') == 'SELECT "select" FROM t'


def test_keyword_casing_respects_oracle_identifier_characters() -> None:
    """`select_count` and `my$select` are identifiers, not keywords."""
    source = "x := select_count + my$select + a#select;"
    assert normalize_keyword_case(source) == source


# --------------------------------------------------------------------------- storage removal


@pytest.mark.parametrize(
    ("source", "gone"),
    [
        ("CREATE TABLE t (c NUMBER) TABLESPACE users;", "TABLESPACE"),
        ('CREATE TABLE t (c NUMBER) TABLESPACE "USERS";', "TABLESPACE"),
        ("CREATE TABLE t (c NUMBER) STORAGE(INITIAL 64K NEXT 1M);", "STORAGE"),
        ("CREATE TABLE t (c NUMBER) PCTFREE 10 PCTUSED 40;", "PCTFREE"),
        ("CREATE TABLE t (c NUMBER) SEGMENT CREATION IMMEDIATE;", "SEGMENT"),
        ("CREATE TABLE t (c NUMBER) NOCOMPRESS LOGGING NOCACHE;", "NOCOMPRESS"),
        ("CREATE TABLE t (c NUMBER) NOPARALLEL;", "NOPARALLEL"),
        ("CREATE TABLE t (c NUMBER) PARALLEL 4;", "PARALLEL"),
    ],
)
def test_storage_clauses_are_removed(source: str, gone: str) -> None:
    """Environment-specific noise is stripped from real DDL tails."""
    assert gone not in remove_storage_clauses(source).upper()


def test_storage_is_removed_as_a_balanced_unit() -> None:
    """Nested parentheses must not leave a dangling bracket behind."""
    source = "CREATE TABLE t (c NUMBER) STORAGE(INITIAL 64K BUFFER_POOL DEFAULT) LOGGING;"
    result = remove_storage_clauses(source)
    assert result.count("(") == result.count(")")
    assert "BUFFER_POOL" not in result


def test_lob_storage_clause_is_removed() -> None:
    """LOB storage clauses are removed with their balanced body."""
    source = 'CREATE TABLE t (c CLOB) LOB ("C") STORE AS (TABLESPACE users ENABLE STORAGE IN ROW);'
    result = remove_storage_clauses(source)
    assert "STORE AS" not in result.upper()
    assert result.count("(") == result.count(")")


def test_identifiers_containing_a_clause_name_are_not_touched() -> None:
    """`CACHE_SIZE` and `LOGGING$` are column names, not clauses."""
    source = "SELECT cache_size, logging$, my#cache FROM t;"
    assert remove_storage_clauses(source) == source


@pytest.mark.parametrize(
    "identifier", ['"cache"', '"CACHE"', '"TABLESPACE"', '"LOGGING"', '"PCTFREE"']
)
def test_a_quoted_identifier_may_itself_be_a_clause_word(identifier: str) -> None:
    """A column legally named "cache" must survive intact.

    Quoted identifiers have to be visible to the remover so that
    `TABLESPACE "USERS"` is removed as a unit — but a clause word must only
    ever match as a *keyword*, never inside a name.
    """
    source = f"SELECT t.{identifier} FROM t;"  # noqa: S608
    assert remove_storage_clauses(source) == source
    assert identifier in _normalize(source)


def test_a_clause_argument_that_is_a_quoted_identifier_is_still_removed() -> None:
    """Masking identifiers must not stop `TABLESPACE "USERS"` being stripped."""
    result = remove_storage_clauses('CREATE TABLE t (c NUMBER) TABLESPACE "USERS";')
    assert "TABLESPACE" not in result.upper()
    assert '"USERS"' not in result


def test_unbalanced_input_is_never_truncated() -> None:
    """Leaving a clause in place is far better than chopping off an object.

    The balanced remover bails out rather than consuming to end-of-input, so
    the ``STORAGE(`` opener and everything after it survive.
    """
    source = "CREATE TABLE t (c NUMBER) STORAGE(INITIAL 64K"
    result = remove_storage_clauses(source)
    assert "STORAGE(" in result
    assert result.startswith("CREATE TABLE t (c NUMBER)")


# --------------------------------------------------------------------------- identifiers


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('"EMPLOYEES"', "EMPLOYEES"),
        ('"HR"."EMPLOYEES"', "HR.EMPLOYEES"),
        ('"A$B#C_1"', "A$B#C_1"),
        ('"lower"', '"lower"'),
        ('"Mixed"', '"Mixed"'),
        ('"WITH SPACE"', '"WITH SPACE"'),
        ('"SELECT"', '"SELECT"'),
        ('"1STARTS_WITH_DIGIT"', '"1STARTS_WITH_DIGIT"'),
        ('""', '""'),
    ],
)
def test_quoted_identifiers_are_unquoted_only_when_safe(source: str, expected: str) -> None:
    """Unquoting must never change which object a name refers to."""
    assert normalize_quoted_identifiers(source) == expected


def test_quoted_identifier_inside_a_literal_is_untouched() -> None:
    """A double quote inside a string is data, not an identifier."""
    source = """SELECT 'he said "HELLO"' FROM dual"""
    assert normalize_quoted_identifiers(source) == source


# --------------------------------------------------------------------------- terminator


@pytest.mark.parametrize("object_type", sorted(PLSQL_TERMINATED_TYPES))
def test_plsql_objects_terminate_with_a_slash(object_type: ObjectType) -> None:
    """A package body ends in END; and needs a lone slash to execute."""
    assert _normalize("CREATE OR REPLACE PACKAGE p AS END;", object_type).endswith("END;\n/\n")


@pytest.mark.parametrize(
    "object_type",
    [ObjectType.VIEW, ObjectType.TABLE, ObjectType.SEQUENCE, ObjectType.SYNONYM],
)
def test_sql_objects_terminate_with_a_semicolon(object_type: ObjectType) -> None:
    """A plain SQL statement ends in a semicolon, never a slash."""
    result = _normalize("CREATE TABLE t (c NUMBER)", object_type)
    assert result.endswith(");\n")


def test_repeated_terminators_collapse() -> None:
    """Two runs of the same object must not accumulate terminators."""
    assert _normalize("BEGIN NULL; END;\n/\n/\n", ObjectType.PROCEDURE).endswith("END;\n/\n")
    assert _normalize("SELECT 1 FROM dual;;;", ObjectType.VIEW).endswith("dual;\n")


def test_unknown_type_leaves_the_terminator_alone() -> None:
    """With no object type, an existing terminator is tidied but not changed."""
    assert _normalize("SELECT 1 FROM dual;").endswith(";\n")
    assert _normalize("BEGIN NULL; END;\n/").endswith("\n/\n")


# --------------------------------------------------------------------------- logic preservation


def _code_tokens(source: str) -> list[str]:
    """The upper-cased token stream of code regions only."""
    tokens: list[str] = []
    for kind, start, end in scan_regions(source):
        if kind is Region.CODE:
            tokens += re.findall(r"[A-Za-z][\w$#]*|\d+|[^\s\w]", source[start:end])
    return [token.upper() for token in tokens]


LOGIC_CORPUS = (
    "CREATE OR REPLACE PROCEDURE p IS\nBEGIN\n  IF x > 1 THEN NULL; END IF;\nEND;",
    "CREATE OR REPLACE FUNCTION f RETURN NUMBER IS\nBEGIN\n  RETURN 1 + 2 * 3;\nEND;",
    "CREATE OR REPLACE PACKAGE BODY b AS\n  PROCEDURE q IS BEGIN\n"
    "    FOR r IN (SELECT id FROM t WHERE c = 'x') LOOP NULL; END LOOP;\n  END;\nEND;",
    "CREATE OR REPLACE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW\n"
    "BEGIN :NEW.id := seq.NEXTVAL; END;",
)


@pytest.mark.parametrize("source", LOGIC_CORPUS)
def test_normalization_preserves_the_code_token_stream(source: str) -> None:
    """No PL/SQL logic is altered: the code tokens come out identical.

    Case and whitespace change by design, so tokens are compared upper-cased.
    """
    assert _code_tokens(_normalize(source)) == _code_tokens(source)


@pytest.mark.parametrize("source", LOGIC_CORPUS)
def test_normalization_preserves_the_token_stream_under_ugly_input(source: str) -> None:
    """Tabs, CRLF, and trailing spaces are noise; the tokens must not move."""
    ugly = source.replace("\n", "  \r\n\t")
    assert _code_tokens(_normalize(ugly)) == _code_tokens(source)


# --------------------------------------------------------------------------- rule switches


def test_every_pipeline_step_has_a_switch() -> None:
    """Each rule is switchable, so a diff can be traced to a config change."""
    assert set(NormalizationSettings.__dataclass_fields__) >= set(PIPELINE_STEPS)


def test_disabling_keyword_casing_leaves_the_body_alone() -> None:
    """The header still normalizes; the body keeps its original casing."""
    off = replace(NormalizationSettings(), normalize_keyword_case=False)
    result = DdlNormalizer(off).normalize("create or replace package p as end;")
    assert result.startswith("CREATE OR REPLACE PACKAGE")  # header rule, still on
    assert " as end;" in result  # casing rule, off


def test_disabling_storage_removal_keeps_the_clause() -> None:
    """The switch really controls the rule."""
    off = replace(NormalizationSettings(), remove_storage_clauses=False)
    result = DdlNormalizer(off).normalize("CREATE TABLE t (c NUMBER) TABLESPACE users;")
    assert "TABLESPACE" in result.upper()


def test_disabling_every_rule_is_a_near_identity() -> None:
    """With all rules off the text is returned unchanged."""
    off = NormalizationSettings(**{
        field: False
        for field, spec in NormalizationSettings.__dataclass_fields__.items()
        if spec.type != "int"
    })
    source = "create   or replace\tpackage p as end;\r\n\r\n\r\n"
    assert DdlNormalizer(off).normalize(source) == source


# --------------------------------------------------------------------------- whitespace


def test_tabs_inside_a_literal_survive() -> None:
    """A tab inside a string is data, not layout."""
    source = "SELECT 'a\tb' FROM dual;"
    assert "'a\tb'" in _normalize(source)


def test_tabs_outside_a_literal_become_spaces() -> None:
    """Layout tabs are expanded so indentation cannot drift."""
    assert "\t" not in _normalize("SELECT\t1 FROM dual;")


def test_tab_expansion_is_fixed_width_regardless_of_literals() -> None:
    """The rule must not change behaviour just because a string is present."""
    plain = _normalize("a\tb\n")
    with_literal = _normalize("a\tb 'x'\n")
    assert plain.startswith("a    b")
    assert with_literal.startswith("a    b")


def test_repeated_blank_lines_collapse() -> None:
    """Three blank lines become one."""
    assert "\n\n\n" not in _normalize("SELECT 1\n\n\n\n FROM dual;")


def test_trailing_whitespace_is_stripped() -> None:
    """Trailing spaces are invisible churn in a diff."""
    result = _normalize("SELECT 1   \nFROM dual;   \n")
    assert not any(line != line.rstrip() for line in result.split("\n"))


def test_transform_code_is_a_no_op_for_the_identity_function() -> None:
    """The rebuild must reproduce the input exactly when nothing changes."""
    source = "SELECT 'a' -- c\n/* b */ \"Q\" FROM dual"
    assert transform_code(source, lambda text: text) == source
