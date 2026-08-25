"""Project exception hierarchy (docs/architecture.md §14.1).

Foundation layer: imports nothing from the project. Every error raised by the
application derives from :class:`OracleVersioningError`, so no call site needs a
bare ``except:`` and no failure can be silently swallowed (CLAUDE.md §7.3).

Blast radius is encoded in the hierarchy, not in the call sites:

===============================  ==========================================
Exception                        Scope
===============================  ==========================================
``ConfigurationError``           execution-fatal, exit 2
``StateError``                   execution-fatal, exit 2
``DatabaseError``                run-unit fatal
``ObjectError``                  object-scoped
``RepositoryError``              object-scoped, or run-unit for reconciliation
``GitError``                     execution-fatal
===============================  ==========================================
"""

__all__ = [
    "CommitError",
    "ConfigurationError",
    "ConnectionError",
    "DatabaseError",
    "DiscoveryError",
    "ExtractionError",
    "GitError",
    "NormalizationError",
    "ObjectError",
    "OracleVersioningError",
    "PushError",
    "RepositoryError",
    "StateError",
]


class OracleVersioningError(Exception):
    """Base class for every error raised by this application."""


class ConfigurationError(OracleVersioningError):
    """Configuration is missing, malformed, or internally contradictory.

    Execution-fatal: raised before any database connection is attempted, and
    mapped to exit code 2.
    """


class StateError(OracleVersioningError):
    """``execution_state.json`` is unreadable or unparseable.

    Execution-fatal (exit code 2). Never recovered from by recreating the file:
    a missing watermark would silently trigger a full export and mask data loss
    (docs/architecture.md §14.3).
    """


class DatabaseError(OracleVersioningError):
    """A failure scoped to one database; other databases continue."""


class ConnectionError(DatabaseError):  # noqa: A001 - shadows the builtin by design
    """A connection or session could not be established or was lost."""


class DiscoveryError(DatabaseError):
    """The ``DBA_OBJECTS`` discovery or inventory query failed."""


class ObjectError(OracleVersioningError):
    """A failure scoped to a single Oracle object; the run unit continues."""


class ExtractionError(ObjectError):
    """Source could not be extracted, or failed validation before writing."""


class NormalizationError(ObjectError):
    """Normalization of an extracted source failed."""


class RepositoryError(OracleVersioningError):
    """A filesystem mapping, write, or reconciliation failure."""


class GitError(OracleVersioningError):
    """A Git failure. Execution-fatal: state is never advanced."""


class CommitError(GitError):
    """Staging or committing failed."""


class PushError(GitError):
    """The push failed after the retry budget was exhausted."""
