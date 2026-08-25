"""Frozen configuration dataclasses (docs/architecture.md §2, §12).

Pure structure — no parsing, no validation, no secret resolution. Those belong
to :mod:`src.config.loader` and :mod:`src.config.secrets`.

Note that ``DatabaseConfig`` holds secret *reference names*, never secret
values (CLAUDE.md §5.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.models.oracle_object import ObjectType

__all__ = [
    "AppConfig",
    "DatabaseConfig",
    "GitSettings",
    "NormalizationSettings",
    "ObjectTypeFilter",
    "ReconciliationSettings",
    "RetrySettings",
    "SchemaFilter",
    "Settings",
]


@dataclass(frozen=True, slots=True)
class SchemaFilter:
    """Include/exclude schema filters, applied in SQL (docs/architecture.md §4.1).

    An empty ``include`` means "every schema that survives ``exclude``".
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ObjectTypeFilter:
    """The object types in scope for a database. Defaults to all twelve."""

    types: tuple[ObjectType, ...] = tuple(ObjectType)


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """One configured Oracle database (``config/databases.yml``)."""

    name: str
    git_folder: str
    dsn_env: str
    username_env: str
    password_env: str
    enabled: bool = True
    thick_mode: bool = False
    schemas: SchemaFilter = field(default_factory=SchemaFilter)
    object_types: ObjectTypeFilter = field(default_factory=ObjectTypeFilter)


@dataclass(frozen=True, slots=True)
class NormalizationSettings:
    """Individually switchable normalization rules (docs/architecture.md §6.4).

    All default to enabled; every switch is logged so an unexpected diff can be
    traced back to a configuration change.
    """

    unify_line_endings: bool = True
    expand_tabs: bool = True
    tab_width: int = 4
    strip_trailing_whitespace: bool = True
    remove_storage_clauses: bool = True
    normalize_create_or_replace: bool = True
    normalize_quoted_identifiers: bool = True
    normalize_keyword_case: bool = True
    collapse_blank_lines: bool = True
    normalize_terminator: bool = True
    ensure_trailing_newline: bool = True


@dataclass(frozen=True, slots=True)
class ReconciliationSettings:
    """Deleted-object reconciliation policy (docs/architecture.md §9, D-1/D-6)."""

    enabled: bool = True
    mass_delete_threshold_percent: float = 10.0
    allow_mass_delete: bool = False


@dataclass(frozen=True, slots=True)
class RetrySettings:
    """Retry budgets (docs/architecture.md §15)."""

    connect_attempts: int = 3
    connect_backoff_seconds: float = 2.0
    query_attempts: int = 2
    extraction_attempts: int = 1
    push_attempts: int = 3
    push_backoff_seconds: float = 1.0
    jitter_ratio: float = 0.2
    run_unit_timeout_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class GitSettings:
    """Git phase settings (docs/architecture.md §11)."""

    branch: str = "main"
    remote: str = "origin"
    author_name: str = "oracle-versioning-bot"
    author_email: str = "oracle-versioning-bot@example.invalid"
    # A reference NAME, never a value: the token itself lives in a CI/CD
    # variable and is resolved at push time (CLAUDE.md §5.3).
    token_env: str = "GIT_PUSH_TOKEN"  # noqa: S105
    skip_ci_marker: str = "[skip ci]"


@dataclass(frozen=True, slots=True)
class Settings:
    """Global behaviour (``config/settings.yml``)."""

    max_workers: int = 4
    watermark_overlap_minutes: int = 5
    arraysize: int = 1000
    prefetchrows: int = 1000
    repository_root: str = "repository"
    state_file: str = "state/execution_state.json"
    normalization: NormalizationSettings = field(default_factory=NormalizationSettings)
    reconciliation: ReconciliationSettings = field(default_factory=ReconciliationSettings)
    retry: RetrySettings = field(default_factory=RetrySettings)
    git: GitSettings = field(default_factory=GitSettings)


@dataclass(frozen=True, slots=True)
class AppConfig:
    """The fully resolved configuration for one execution."""

    databases: tuple[DatabaseConfig, ...]
    settings: Settings
    dry_run: bool = False
    no_git: bool = False

    @property
    def enabled_databases(self) -> tuple[DatabaseConfig, ...]:
        """The subset of configured databases that are enabled."""
        return tuple(db for db in self.databases if db.enabled)
