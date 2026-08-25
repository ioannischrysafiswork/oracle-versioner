"""Application entry point — orchestration only.

**No business logic lives here** (CLAUDE.md §7.3, docs/requirements.md §24).
This module parses the CLI, configures logging, wires collaborators, invokes
the pipeline, and translates the outcome into an exit code. Nothing else.

Exit codes (docs/architecture.md §14.5)::

    0   success, including "nothing changed"
    1   one or more databases failed, or commit/push failed
    2   configuration invalid or state file unparseable

Run with::

    python -m src.main --dry-run
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from src import __version__
from src.config.loader import ConfigLoader
from src.exceptions import ConfigurationError, OracleVersioningError, StateError
from src.logging.logger import configure_logging, get_logger
from src.models.execution import ExitCode
from src.state.execution_state import ExecutionStateStore
from src.workflow.pipeline import Pipeline

__all__ = ["build_parser", "main"]

_log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Construct the command-line parser.

    Returns:
        The parser for this application's arguments.
    """
    parser = argparse.ArgumentParser(
        prog="oracle-versioner",
        description="Synchronize Oracle database source code with a GitLab repository.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="directory holding databases.yml and settings.yml (default: config)",
    )
    parser.add_argument(
        "--database",
        action="append",
        dest="databases",
        metavar="NAME",
        help="restrict the run to this database; repeatable (default: all enabled)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="connect and extract, but write no file, create no commit, and update no state",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="write files but skip commit and push",
    )
    parser.add_argument(
        "--allow-mass-delete",
        action="store_true",
        help="permit reconciliation to exceed the mass-deletion safety threshold",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log level; overrides config/logging.yml (default: that file, else INFO)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one synchronization and return the process exit code.

    Args:
        argv: Command-line arguments, excluding the program name. Defaults to
            ``sys.argv[1:]``.

    Returns:
        An :class:`~src.models.execution.ExitCode` value.
    """
    args = build_parser().parse_args(argv)
    execution_id = uuid.uuid4().hex[:12]
    redactor = configure_logging(
        args.log_level,
        execution_id=execution_id,
        config_path=args.config_dir / "logging.yml",
    )

    try:
        config = ConfigLoader(
            args.config_dir, secret_sink=redactor.add_secret
        ).load(
            dry_run=args.dry_run,
            no_git=args.no_git,
            allow_mass_delete=args.allow_mass_delete,
            only_databases=args.databases,
        )
        state_store = ExecutionStateStore(Path(config.settings.state_file))
        state = state_store.load()
        result = Pipeline(config, state, state_store, execution_id).run()
    except (ConfigurationError, StateError):
        _log.exception("execution aborted: invalid configuration or state")
        return int(ExitCode.CONFIGURATION)
    except OracleVersioningError:
        _log.exception("execution failed")
        return int(ExitCode.FAILURE)

    return int(result.exit_code)


if __name__ == "__main__":
    sys.exit(main())
