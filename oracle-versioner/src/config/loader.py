"""Read and validate YAML configuration (docs/architecture.md §2, §12).

Orchestration only: read the files, apply precedence, resolve secrets. The
validation rules themselves live in :mod:`src.config.validation`.

Validation runs at start-up, before any connection is attempted, and is fatal
(exit code 2, nothing touched) on: malformed YAML, unknown keys, wrong value
types, duplicate database names, unknown object types, missing or unresolvable
secret references, invalid git folder paths, and contradictory include/exclude
filters.

Precedence (§12.3): ``CLI > environment > settings.yml > built-in defaults``.

Secret **values** never enter the returned configuration. References are
resolved once, purely to fail fast on a missing secret, and each resolved value
is handed to ``secret_sink`` so the log redaction filter learns it before any
connection is attempted. The value is then dropped.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from pathlib import Path

from src.config.models import (
    AppConfig,
    DatabaseConfig,
    GitSettings,
    NormalizationSettings,
    ReconciliationSettings,
    RetrySettings,
    Settings,
)
from src.config.secrets import EnvironmentSecretProvider, SecretProvider
from src.config.validation import (
    ENV_OVERRIDES,
    SECRET_KEYS,
    apply_env_overrides,
    build_database,
    build_settings_section,
    read_yaml,
)
from src.exceptions import ConfigurationError

__all__ = ["ENV_OVERRIDES", "ConfigLoader"]

_SECTIONS: dict[str, type] = {
    "normalization": NormalizationSettings,
    "reconciliation": ReconciliationSettings,
    "retry": RetrySettings,
    "git": GitSettings,
}


class ConfigLoader:
    """Loads ``databases.yml`` and ``settings.yml`` into an :class:`AppConfig`."""

    def __init__(
        self,
        config_dir: Path,
        secret_provider: SecretProvider | None = None,
        secret_sink: Callable[[str], None] | None = None,
    ) -> None:
        """Create a loader.

        Args:
            config_dir: Directory holding ``databases.yml`` and ``settings.yml``.
            secret_provider: Resolver for secret reference names. Defaults to
                the environment provider.
            secret_sink: Receives every resolved secret value so it can be
                registered for log redaction. Defaults to discarding them.
        """
        self.config_dir = config_dir
        self.secret_provider: SecretProvider = secret_provider or EnvironmentSecretProvider()
        self.secret_sink = secret_sink or (lambda _value: None)

    def load(
        self,
        *,
        dry_run: bool = False,
        no_git: bool = False,
        allow_mass_delete: bool = False,
        only_databases: Iterable[str] | None = None,
    ) -> AppConfig:
        """Load, validate, and return the configuration for this execution.

        Args:
            dry_run: Whether the execution suppresses all writes.
            no_git: Whether the execution writes files but skips commit/push.
            allow_mass_delete: CLI override for the mass-deletion threshold.
            only_databases: Restrict the run to these database names.

        Returns:
            The validated configuration.

        Raises:
            ConfigurationError: On any invalid or unresolvable configuration.
        """
        databases = self._load_databases()
        settings = self._load_settings()

        if allow_mass_delete:
            settings = dataclasses.replace(
                settings,
                reconciliation=dataclasses.replace(settings.reconciliation, allow_mass_delete=True),
            )

        if only_databases is not None:
            wanted = set(only_databases)
            missing = sorted(wanted - {db.name for db in databases})
            if missing:
                raise ConfigurationError(f"--database names not configured: {missing}")
            databases = tuple(db for db in databases if db.name in wanted)

        self._verify_secrets(databases)
        return AppConfig(databases=databases, settings=settings, dry_run=dry_run, no_git=no_git)

    def _load_databases(self) -> tuple[DatabaseConfig, ...]:
        """Parse and validate ``databases.yml``, rejecting duplicate names."""
        document = read_yaml(self.config_dir / "databases.yml")
        unknown = set(document) - {"databases"}
        if unknown:
            raise ConfigurationError(f"databases.yml: unknown key(s) {sorted(unknown)}")

        entries = document.get("databases") or []
        if not isinstance(entries, list):
            raise ConfigurationError("databases.yml: 'databases' must be a list")

        databases = [build_database(entry, index) for index, entry in enumerate(entries)]
        seen: set[str] = set()
        for db in databases:
            if db.name in seen:
                raise ConfigurationError(f"databases.yml: duplicate database name {db.name!r}")
            seen.add(db.name)
        return tuple(databases)

    def _load_settings(self) -> Settings:
        """Parse ``settings.yml``, check ranges, and apply environment overrides."""
        path = self.config_dir / "settings.yml"
        document = read_yaml(path) if path.exists() else {}
        sections = {name: document.pop(name, None) for name in _SECTIONS}

        settings = dataclasses.replace(
            build_settings_section(Settings, document, "settings.yml"),
            **{
                name: build_settings_section(cls, sections[name], f"settings.yml.{name}")
                for name, cls in _SECTIONS.items()
            },
        )

        if settings.max_workers < 1:
            raise ConfigurationError("settings.yml.max_workers: must be at least 1")
        if settings.watermark_overlap_minutes < 0:
            raise ConfigurationError(
                "settings.yml.watermark_overlap_minutes: must not be negative"
            )
        if not 0 <= settings.reconciliation.mass_delete_threshold_percent <= 100:
            raise ConfigurationError(
                "settings.yml.reconciliation.mass_delete_threshold_percent: must be 0-100"
            )
        return apply_env_overrides(settings)

    def _verify_secrets(self, databases: Iterable[DatabaseConfig]) -> None:
        """Resolve every enabled database's references, to fail fast and enable redaction."""
        for db in databases:
            if not db.enabled:
                continue
            for key in SECRET_KEYS:
                self.secret_sink(self.secret_provider.resolve(getattr(db, key)))
