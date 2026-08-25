"""Skeleton integrity: the project imports, the CLI runs, layering holds.

These tests exist to keep the scaffold honest between now and the phase that
fills it in. They assert structure, not behaviour.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import src
from src.main import build_parser, main
from src.models.execution import ExitCode
from tests.conftest import PROJECT_ROOT

ALL_MODULES = [
    "src.exceptions",
    "src.main",
    "src.config.loader",
    "src.config.models",
    "src.config.secrets",
    "src.config.validation",
    "src.git.commit",
    "src.git.git_client",
    "src.git.push",
    "src.logging.logger",
    "src.models.database",
    "src.models.execution",
    "src.models.oracle_object",
    "src.normalizer.ddl_normalizer",
    "src.normalizer.plsql_normalizer",
    "src.normalizer.storage_clause_remover",
    "src.oracle.connection",
    "src.oracle.ddl_extractor",
    "src.oracle.metadata",
    "src.oracle.object_discovery",
    "src.oracle.source_extractor",
    "src.oracle.sql",
    "src.repository.comparer",
    "src.repository.file_mapper",
    "src.repository.writer",
    "src.state.execution_state",
    "src.state.models",
    "src.utils.filesystem",
    "src.utils.hashing",
    "src.utils.retry",
    "src.utils.timers",
    "src.workflow.git_phase",
    "src.workflow.pipeline",
    "src.workflow.run_unit",
]


@pytest.mark.parametrize("module_name", ALL_MODULES)
def test_every_module_imports(module_name: str) -> None:
    """Every module in the skeleton imports cleanly."""
    assert importlib.import_module(module_name) is not None


def test_module_list_is_exhaustive() -> None:
    """ALL_MODULES covers every module actually present under src/."""
    discovered = {
        name
        for _, name, is_pkg in pkgutil.walk_packages(src.__path__, prefix="src.")
        if not is_pkg
    }
    assert discovered == set(ALL_MODULES)


def test_src_logging_does_not_shadow_stdlib() -> None:
    """`src.logging` must not displace the standard library `logging`."""
    import logging as stdlib_logging

    from src.logging import logger

    assert stdlib_logging.__name__ == "logging"
    assert logger.get_logger("x").__class__ is stdlib_logging.Logger


def test_no_module_exceeds_the_size_limit() -> None:
    """No module beyond ~300 lines (CLAUDE.md §7.3)."""
    root = Path(src.__file__).parent
    oversized = {
        path.name: sum(1 for _ in path.read_text(encoding="utf-8").splitlines())
        for path in root.rglob("*.py")
        if sum(1 for _ in path.read_text(encoding="utf-8").splitlines()) > 300
    }
    assert not oversized


def test_cli_parses_the_documented_flags() -> None:
    """The CLI exposes the flags the architecture specifies."""
    args = build_parser().parse_args(
        ["--dry-run", "--no-git", "--allow-mass-delete", "--database", "HRDB"]
    )
    assert args.dry_run is True
    assert args.no_git is True
    assert args.allow_mass_delete is True
    assert args.databases == ["HRDB"]


def test_cli_defaults() -> None:
    """Defaults are safe: no dry-run implied, config read from `config/`."""
    args = build_parser().parse_args([])
    assert args.dry_run is False
    assert args.no_git is False
    assert args.config_dir == Path("config")
    assert args.log_level is None


def test_application_runs_end_to_end_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`python -m src.main --dry-run` completes and reports success."""
    monkeypatch.chdir(tmp_path)
    assert main(["--dry-run", "--config-dir", str(PROJECT_ROOT / "config")]) == ExitCode.SUCCESS


def test_invalid_configuration_exits_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configuration error exits 2 and touches nothing."""
    monkeypatch.chdir(tmp_path)
    assert main(["--config-dir", str(tmp_path / "absent")]) == ExitCode.CONFIGURATION


def test_unparseable_state_exits_two(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A corrupt state file is fatal and is not silently recreated."""
    monkeypatch.chdir(tmp_path)
    state = tmp_path / "state" / "execution_state.json"
    state.parent.mkdir()
    state.write_text("{corrupt", encoding="utf-8")
    exit_code = main(["--config-dir", str(PROJECT_ROOT / "config")])
    assert exit_code == ExitCode.CONFIGURATION
    assert state.read_text(encoding="utf-8") == "{corrupt"
