"""Path mapping, writing, and reconciliation.

The mapping's **invertibility** is what deletion detection rests on: if
``to_object(to_path(x)) != x`` for any object, reconciliation can delete a live
object's file. That property is tested directly, and again as a round-trip over
every object type.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.config.models import ReconciliationSettings, SchemaFilter
from src.exceptions import RepositoryError
from src.models.oracle_object import ObjectType, OracleObject
from src.repository.comparer import Reconciler
from src.repository.file_mapper import (
    FOLDER_BY_TYPE,
    ILLEGAL_NAME_CHARACTERS,
    TYPE_BY_FOLDER,
    WINDOWS_RESERVED_NAMES,
    FileMapper,
)
from src.repository.writer import FileWriter, NullWriter, enforce_conventions


def _obj(object_type: ObjectType, name: str = "EMPLOYEE_PKG", owner: str = "HR") -> OracleObject:
    return OracleObject(
        owner=owner, object_type=object_type, object_name=name, database="TESTDB"
    )


@pytest.fixture
def mapper(tmp_path: Path) -> FileMapper:
    """A mapper rooted at an empty database subtree."""
    return FileMapper(tmp_path / "TESTDB", "TESTDB")


# --------------------------------------------------------------------------- folder table


def test_folder_table_is_total_over_object_types() -> None:
    """Every supported type maps to a folder."""
    assert set(FOLDER_BY_TYPE) == set(ObjectType)


def test_folder_names_are_unique() -> None:
    """Distinct types never share a folder — a spec and its body must not collide."""
    assert len(set(FOLDER_BY_TYPE.values())) == len(FOLDER_BY_TYPE)
    assert FOLDER_BY_TYPE[ObjectType.PACKAGE] != FOLDER_BY_TYPE[ObjectType.PACKAGE_BODY]
    assert FOLDER_BY_TYPE[ObjectType.TYPE] != FOLDER_BY_TYPE[ObjectType.TYPE_BODY]


def test_folder_names_match_the_specification() -> None:
    """Folder names are exactly those in docs/architecture.md §10.1."""
    assert FOLDER_BY_TYPE == {
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


def test_folder_table_and_its_inverse_stay_in_step() -> None:
    """The inverse table *is* the mapping's invertibility."""
    assert {v: k for k, v in FOLDER_BY_TYPE.items()} == TYPE_BY_FOLDER


# --------------------------------------------------------------------------- mapping


@pytest.mark.parametrize("object_type", sorted(ObjectType))
def test_every_type_maps_to_the_expected_path(
    object_type: ObjectType, mapper: FileMapper
) -> None:
    """The layout is <database>/<schema>/<type_folder>/<NAME>.sql."""
    path = mapper.to_path(_obj(object_type, "OBJ"))
    assert path == mapper.database_root / "HR" / FOLDER_BY_TYPE[object_type] / "OBJ.sql"


@pytest.mark.parametrize("object_type", sorted(ObjectType))
def test_mapping_is_invertible(object_type: ObjectType, mapper: FileMapper) -> None:
    """Deletion detection depends on this round-trip holding for every type."""
    original = _obj(object_type, "OBJ")
    assert mapper.to_object(mapper.to_path(original)) == original


def test_object_names_are_never_rewritten(mapper: FileMapper) -> None:
    """The name on disk is the Oracle name, so the mapping stays predictable."""
    assert mapper.to_path(_obj(ObjectType.PACKAGE, "Mixed_Case$1")).stem == "Mixed_Case$1"


def test_extension_is_lowercase_sql(mapper: FileMapper) -> None:
    """Case-consistent across case-sensitive and case-insensitive filesystems."""
    assert mapper.to_path(_obj(ObjectType.VIEW)).suffix == ".sql"


@pytest.mark.parametrize("char", sorted(ILLEGAL_NAME_CHARACTERS))
def test_illegal_characters_fail_loudly(char: str, mapper: FileMapper) -> None:
    """Decision D-5: fail the object, never silently encode the name."""
    with pytest.raises(RepositoryError, match="illegal character"):
        mapper.to_path(_obj(ObjectType.PACKAGE, f"BAD{char}NAME"))


@pytest.mark.parametrize("name", ["TRAILING.", "TRAILING ", "WITH\tTAB", "WITH\nNEWLINE"])
def test_other_unsafe_names_fail_loudly(name: str, mapper: FileMapper) -> None:
    """Control characters and trailing dots or spaces are unmappable."""
    with pytest.raises(RepositoryError):
        mapper.to_path(_obj(ObjectType.PACKAGE, name))


@pytest.mark.parametrize("name", ["CON", "PRN", "NUL", "COM1", "LPT9", "con"])
def test_windows_reserved_names_fail_loudly(name: str, mapper: FileMapper) -> None:
    """A repository containing CON.sql cannot be checked out on Windows at all."""
    with pytest.raises(RepositoryError, match="reserved device name"):
        mapper.to_path(_obj(ObjectType.PACKAGE, name))
    assert name.upper() in WINDOWS_RESERVED_NAMES


def test_illegal_schema_name_fails_loudly(mapper: FileMapper) -> None:
    """The schema is a path component too."""
    with pytest.raises(RepositoryError, match="schema name"):
        mapper.to_path(_obj(ObjectType.PACKAGE, "OK", owner="BAD/OWNER"))


@pytest.mark.parametrize(
    "path_parts",
    [
        ("HR", "packages"),
        ("HR", "packages", "P.txt"),
        ("HR", "not_a_type", "P.sql"),
        ("HR", "packages", "sub", "P.sql"),
    ],
)
def test_invalid_paths_do_not_invert(path_parts: tuple[str, ...], mapper: FileMapper) -> None:
    """A path that is not a valid mapping is rejected, never guessed at."""
    with pytest.raises(RepositoryError):
        mapper.to_object(mapper.database_root.joinpath(*path_parts))


def test_absolute_path_inverts_against_a_relative_root(monkeypatch, tmp_path: Path) -> None:
    """Git reports staged paths absolute; the mapper root is configured relative.

    Regression: found against a live database. Comparing them unresolved made
    every inversion fail, which silently emptied the object list out of the
    commit body — the run looked fine and the message said "0 objects".
    """
    monkeypatch.chdir(tmp_path)
    relative_mapper = FileMapper(Path("repository") / "TESTDB", "TESTDB")
    absolute = tmp_path / "repository" / "TESTDB" / "HR" / "packages" / "P.sql"

    assert relative_mapper.to_object(absolute).object_name == "P"


def test_path_outside_the_database_root_is_rejected(mapper: FileMapper, tmp_path: Path) -> None:
    """Reconciliation must never consider a path from another database."""
    with pytest.raises(RepositoryError, match="not under"):
        mapper.to_object(tmp_path / "OTHERDB" / "HR" / "packages" / "P.sql")


def test_case_insensitive_collisions_fail_loudly(mapper: FileMapper) -> None:
    """Oracle allows "Foo" and "FOO"; a Windows filesystem does not."""
    with pytest.raises(RepositoryError, match="collides"):
        mapper.map_all([_obj(ObjectType.PACKAGE, "Foo"), _obj(ObjectType.PACKAGE, "FOO")])


def test_map_all_returns_one_entry_per_object(mapper: FileMapper) -> None:
    """The happy path maps every object without complaint."""
    objects = [_obj(ObjectType.PACKAGE, "A"), _obj(ObjectType.VIEW, "B")]
    assert len(mapper.map_all(objects)) == 2


# --------------------------------------------------------------------------- writer


def test_writer_creates_directories_and_writes(tmp_path: Path) -> None:
    """A new schema or object type needs no special handling."""
    target = tmp_path / "TESTDB" / "HR" / "packages" / "P.sql"
    FileWriter().write(target, "CREATE OR REPLACE PACKAGE p AS END;\n")
    assert target.read_text(encoding="utf-8") == "CREATE OR REPLACE PACKAGE p AS END;\n"


def test_writer_enforces_lf_and_no_bom(tmp_path: Path) -> None:
    """A BOM or a CRLF on disk is a phantom diff in every future run."""
    target = tmp_path / "P.sql"
    FileWriter().write(target, "﻿line one\r\nline two\r\n")
    raw = target.read_bytes()
    assert raw == b"line one\nline two\n"


@pytest.mark.parametrize("content", ["x", "x\n", "x\n\n\n"])
def test_writer_enforces_exactly_one_trailing_newline(content: str, tmp_path: Path) -> None:
    r"""Avoids "\ No newline at end of file" churn in both directions."""
    target = tmp_path / "P.sql"
    FileWriter().write(target, content)
    assert target.read_bytes() == b"x\n"


def test_writer_overwrites_rather_than_appends(tmp_path: Path) -> None:
    """A changed object replaces its file completely."""
    target = tmp_path / "P.sql"
    FileWriter().write(target, "first\n")
    FileWriter().write(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"


@pytest.mark.parametrize("content", ["", "   ", "\n\n"])
def test_writer_refuses_empty_content(content: str, tmp_path: Path) -> None:
    """A failed extraction must never blank out a good file."""
    with pytest.raises(RepositoryError, match="empty content"):
        FileWriter().write(tmp_path / "P.sql", content)


def test_writer_leaves_no_temporary_files(tmp_path: Path) -> None:
    """The write is atomic: temp file, then rename."""
    FileWriter().write(tmp_path / "P.sql", "x\n")
    assert [p.name for p in tmp_path.iterdir()] == ["P.sql"]


def test_null_writer_touches_nothing(tmp_path: Path) -> None:
    """Dry-run suppression is a substituted collaborator, not a conditional."""
    writer = NullWriter()
    target = tmp_path / "HRDB" / "HR" / "packages" / "P.sql"
    writer.write(target, "CREATE OR REPLACE PACKAGE p AS END;\n")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
    assert writer.would_write == [target]


def test_null_writer_still_validates(tmp_path: Path) -> None:
    """A dry run reports the same failures a real run would."""
    with pytest.raises(RepositoryError):
        NullWriter().write(tmp_path / "P.sql", "  ")


def test_enforce_conventions_is_idempotent() -> None:
    """Applying the on-disk form twice changes nothing."""
    once = enforce_conventions("a\r\nb")
    assert enforce_conventions(once) == once


# --------------------------------------------------------------------------- reconciliation


def _touch(root: Path, *relative: str) -> Path:
    path = root.joinpath(*relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    return path


def test_reconciler_finds_files_with_no_matching_object(mapper: FileMapper) -> None:
    """The core difference: on disk but not in the inventory."""
    live = _touch(mapper.database_root, "HR", "packages", "LIVE.sql")
    dropped = _touch(mapper.database_root, "HR", "packages", "DROPPED.sql")
    reconciler = Reconciler(ReconciliationSettings(), mapper)

    obsolete = reconciler.obsolete_paths(
        [_obj(ObjectType.PACKAGE, "LIVE")], [live, dropped]
    )
    assert obsolete == [dropped]


def test_reconciler_compares_paths_not_contents(mapper: FileMapper) -> None:
    """A file whose content differs is NOT a deletion candidate."""
    path = _touch(mapper.database_root, "HR", "packages", "LIVE.sql")
    path.write_text("completely different content\n", encoding="utf-8")
    reconciler = Reconciler(ReconciliationSettings(), mapper)
    assert reconciler.obsolete_paths([_obj(ObjectType.PACKAGE, "LIVE")], [path]) == []


def test_enumerate_returns_nothing_before_the_first_export(mapper: FileMapper) -> None:
    """A missing subtree is the normal state on a first run, not an error."""
    assert Reconciler(ReconciliationSettings(), mapper).enumerate_paths() == []


def test_enumerate_only_walks_in_scope_schemas(mapper: FileMapper) -> None:
    """Removing a schema from the include filter must not delete its history."""
    _touch(mapper.database_root, "HR", "packages", "A.sql")
    _touch(mapper.database_root, "PAYROLL", "packages", "B.sql")
    reconciler = Reconciler(ReconciliationSettings(), mapper)

    in_scope = reconciler.enumerate_paths(SchemaFilter(include=("HR",)))
    assert [p.name for p in in_scope] == ["A.sql"]


def test_enumerate_honours_the_exclude_filter(mapper: FileMapper) -> None:
    """An excluded schema's files are never deletion candidates."""
    _touch(mapper.database_root, "HR", "packages", "A.sql")
    _touch(mapper.database_root, "SCRATCH", "packages", "B.sql")
    reconciler = Reconciler(ReconciliationSettings(), mapper)

    found = reconciler.enumerate_paths(SchemaFilter(exclude=("SCRATCH",)))
    assert [p.name for p in found] == ["A.sql"]


def test_enumerate_covers_every_schema_when_no_include_filter(mapper: FileMapper) -> None:
    """An empty include list means every schema present."""
    _touch(mapper.database_root, "HR", "packages", "A.sql")
    _touch(mapper.database_root, "PAYROLL", "views", "B.sql")
    found = Reconciler(ReconciliationSettings(), mapper).enumerate_paths(SchemaFilter())
    assert [p.name for p in found] == ["A.sql", "B.sql"]


@pytest.mark.parametrize(
    ("deletions", "total", "blocked"),
    [
        (0, 100, False),
        (10, 100, False),   # exactly 10% is allowed
        (11, 100, True),    # over the threshold
        (1, 5, True),       # 20%
        (1, 0, True),       # nothing on disk but something to delete
    ],
)
def test_mass_deletion_threshold(deletions: int, total: int, blocked: bool) -> None:
    """Decision D-6: more than 10% of a database's files blocks all deletions."""
    reconciler = Reconciler(ReconciliationSettings(), None)
    assert reconciler.exceeds_threshold(deletions, total) is blocked


def test_allow_mass_delete_overrides_the_threshold() -> None:
    """A genuine mass drop is a deliberate human action."""
    settings = replace(ReconciliationSettings(), allow_mass_delete=True)
    assert Reconciler(settings, None).exceeds_threshold(99, 100) is False


def test_threshold_default_is_ten_percent() -> None:
    """Decision D-6 is the configured default."""
    assert ReconciliationSettings().mass_delete_threshold_percent == 10.0


def test_reconciler_requires_a_mapper() -> None:
    """Reconciling without a mapper is a programming error, not a silent no-op."""
    with pytest.raises(ValueError, match="FileMapper"):
        Reconciler(ReconciliationSettings(), None).obsolete_paths([], [])
