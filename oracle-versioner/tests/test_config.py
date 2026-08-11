"""Configuration loading, validation, secret resolution, and precedence.

Covers the areas required by CLAUDE.md §6: valid and invalid YAML, missing
secrets, duplicate names, unknown object types, filter contradictions, and
precedence order.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.config.loader import ConfigLoader
from src.config.models import AppConfig, DatabaseConfig, Settings
from src.config.secrets import EnvironmentSecretProvider
from src.exceptions import ConfigurationError
from src.models.oracle_object import ObjectType

VALID_DATABASE = """
databases:
  - name: HRDB
    git_folder: HRDB
    dsn_env: HRDB_DSN
    username_env: HRDB_USER
    password_env: HRDB_PASSWORD
    schemas:
      include: [hr]
      exclude: [SYS]
    object_types: [PACKAGE, "PACKAGE BODY"]
"""


class FakeSecretProvider:
    """Resolves any reference to a predictable value."""

    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known
        self.resolved: list[str] = []

    def resolve(self, name: str) -> str:
        if self.known is not None and name not in self.known:
            raise ConfigurationError(f"secret reference {name!r} is not set in the environment")
        self.resolved.append(name)
        return f"value-of-{name}"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """An empty configuration directory."""
    (tmp_path / "databases.yml").write_text("databases: []\n", encoding="utf-8")
    return tmp_path


def _loader(config_dir: Path, **kwargs) -> ConfigLoader:
    kwargs.setdefault("secret_provider", FakeSecretProvider())
    return ConfigLoader(config_dir, **kwargs)


# --------------------------------------------------------------------------- models


def test_default_object_type_filter_covers_all_twelve_types() -> None:
    """Omitting `object_types` means every supported type is in scope."""
    default = DatabaseConfig(
        name="D", git_folder="D", dsn_env="A", username_env="B", password_env="C"
    )
    assert set(default.object_types.types) == set(ObjectType)
    assert len(ObjectType) == 12


def test_disabled_databases_are_excluded(database_config: DatabaseConfig) -> None:
    """`enabled_databases` filters out disabled entries."""
    disabled = replace(database_config, name="OFF", enabled=False)
    config = AppConfig(databases=(database_config, disabled), settings=Settings())
    assert [db.name for db in config.enabled_databases] == [database_config.name]


def test_approved_defaults_match_the_architecture() -> None:
    """Defaults encode decision D-6 and the documented watermark overlap."""
    settings = Settings()
    assert settings.reconciliation.mass_delete_threshold_percent == 10.0
    assert settings.reconciliation.allow_mass_delete is False
    assert settings.watermark_overlap_minutes == 5


# --------------------------------------------------------------------------- secrets


def test_environment_secret_provider_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """A set environment variable resolves to its value."""
    monkeypatch.setenv("TESTDB_PASSWORD", "value")
    assert EnvironmentSecretProvider().resolve("TESTDB_PASSWORD") == "value"


def test_environment_secret_provider_reports_missing_by_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable secret is fatal, and the error names the variable only."""
    monkeypatch.delenv("TESTDB_PASSWORD", raising=False)
    with pytest.raises(ConfigurationError) as excinfo:
        EnvironmentSecretProvider().resolve("TESTDB_PASSWORD")
    assert "TESTDB_PASSWORD" in str(excinfo.value)


def test_secrets_are_resolved_and_handed_to_the_redaction_sink(config_dir: Path) -> None:
    """Every reference is resolved once, and each value reaches the sink."""
    (config_dir / "databases.yml").write_text(VALID_DATABASE, encoding="utf-8")
    provider = FakeSecretProvider()
    seen: list[str] = []
    ConfigLoader(config_dir, secret_provider=provider, secret_sink=seen.append).load()
    assert provider.resolved == ["HRDB_DSN", "HRDB_USER", "HRDB_PASSWORD"]
    assert seen == ["value-of-HRDB_DSN", "value-of-HRDB_USER", "value-of-HRDB_PASSWORD"]


def test_secret_values_never_enter_the_configuration(config_dir: Path) -> None:
    """The returned config carries reference names only."""
    (config_dir / "databases.yml").write_text(VALID_DATABASE, encoding="utf-8")
    config = _loader(config_dir).load()
    assert "value-of-HRDB_PASSWORD" not in repr(config)
    assert config.databases[0].password_env == "HRDB_PASSWORD"


def test_unresolvable_secret_is_fatal(config_dir: Path) -> None:
    """A missing secret aborts before any connection is attempted."""
    (config_dir / "databases.yml").write_text(VALID_DATABASE, encoding="utf-8")
    loader = ConfigLoader(config_dir, secret_provider=FakeSecretProvider(known={"HRDB_DSN"}))
    with pytest.raises(ConfigurationError):
        loader.load()


def test_disabled_databases_do_not_need_secrets(config_dir: Path) -> None:
    """A disabled database is never connected to, so it needs no secret."""
    (config_dir / "databases.yml").write_text(
        VALID_DATABASE + "    enabled: false\n", encoding="utf-8"
    )
    provider = FakeSecretProvider(known=set())
    assert ConfigLoader(config_dir, secret_provider=provider).load().databases[0].enabled is False


# --------------------------------------------------------------------------- databases.yml


def test_valid_database_parses(config_dir: Path) -> None:
    """A well-formed entry produces the expected configuration."""
    (config_dir / "databases.yml").write_text(VALID_DATABASE, encoding="utf-8")
    db = _loader(config_dir).load().databases[0]
    assert db.name == "HRDB"
    assert db.git_folder == "HRDB"
    assert db.enabled is True
    assert db.schemas.include == ("HR",)  # normalized to upper case
    assert db.schemas.exclude == ("SYS",)
    assert db.object_types.types == (ObjectType.PACKAGE, ObjectType.PACKAGE_BODY)


def test_empty_database_list_is_valid(config_dir: Path) -> None:
    """An empty list is a valid configuration; the agent is simply a no-op."""
    assert _loader(config_dir).load().databases == ()


def test_missing_databases_file_is_fatal(tmp_path: Path) -> None:
    """A missing databases.yml is a configuration error, not an empty run."""
    with pytest.raises(ConfigurationError, match="not found"):
        _loader(tmp_path).load()


def test_malformed_yaml_is_fatal(config_dir: Path) -> None:
    """Invalid YAML aborts the execution."""
    (config_dir / "databases.yml").write_text("databases: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid YAML"):
        _loader(config_dir).load()


def test_duplicate_database_names_are_fatal(config_dir: Path) -> None:
    """Two databases may not share a name — state is keyed by it."""
    (config_dir / "databases.yml").write_text(VALID_DATABASE + VALID_DATABASE.replace(
        "databases:\n", ""
    ), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="duplicate database name"):
        _loader(config_dir).load()


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("    unexpected: 1\n", "unknown key"),
        ("    enabled: yes-please\n", "boolean"),
    ],
)
def test_bad_database_keys_are_fatal(config_dir: Path, mutation: str, match: str) -> None:
    """Unknown keys and wrongly typed values are rejected."""
    (config_dir / "databases.yml").write_text(VALID_DATABASE + mutation, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=match):
        _loader(config_dir).load()


def test_missing_required_key_is_fatal(config_dir: Path) -> None:
    """Every secret reference is required."""
    (config_dir / "databases.yml").write_text(
        VALID_DATABASE.replace("    password_env: HRDB_PASSWORD\n", ""), encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="missing required key"):
        _loader(config_dir).load()


@pytest.mark.parametrize("folder", ["/etc", "../outside", "nested/../../escape", ""])
def test_git_folder_may_not_escape_the_repository(config_dir: Path, folder: str) -> None:
    """A git folder must stay inside the repository."""
    (config_dir / "databases.yml").write_text(
        VALID_DATABASE.replace("git_folder: HRDB", f"git_folder: {folder!r}"), encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="git_folder"):
        _loader(config_dir).load()


def test_unknown_object_type_is_fatal(config_dir: Path) -> None:
    """Only the twelve supported types are accepted."""
    (config_dir / "databases.yml").write_text(
        VALID_DATABASE.replace('[PACKAGE, "PACKAGE BODY"]', "[PACKAGE, JAVA_CLASS]"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unknown object type"):
        _loader(config_dir).load()


def test_contradictory_schema_filters_are_fatal(config_dir: Path) -> None:
    """A schema cannot be both included and excluded."""
    (config_dir / "databases.yml").write_text(
        VALID_DATABASE.replace("exclude: [SYS]", "exclude: [HR]"), encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="both include and exclude"):
        _loader(config_dir).load()


# --------------------------------------------------------------------------- settings.yml


def test_missing_settings_file_falls_back_to_defaults(config_dir: Path) -> None:
    """settings.yml is optional; every value has a built-in default."""
    assert _loader(config_dir).load().settings == Settings()


def test_settings_are_parsed_including_nested_sections(config_dir: Path) -> None:
    """Nested sections merge over the defaults."""
    (config_dir / "settings.yml").write_text(
        "max_workers: 8\nreconciliation:\n  mass_delete_threshold_percent: 25\n",
        encoding="utf-8",
    )
    settings = _loader(config_dir).load().settings
    assert settings.max_workers == 8
    assert settings.reconciliation.mass_delete_threshold_percent == 25.0
    assert settings.reconciliation.enabled is True  # untouched default


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("nonsense: 1\n", "unknown key"),
        ("max_workers: many\n", "expected int"),
        ("max_workers: 0\n", "at least 1"),
        ("watermark_overlap_minutes: -1\n", "must not be negative"),
        ("reconciliation:\n  mass_delete_threshold_percent: 150\n", "must be 0-100"),
        ("reconciliation:\n  nope: 1\n", "unknown key"),
    ],
)
def test_invalid_settings_are_fatal(config_dir: Path, content: str, match: str) -> None:
    """Bad settings abort before anything is touched."""
    (config_dir / "settings.yml").write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=match):
        _loader(config_dir).load()


# --------------------------------------------------------------------------- precedence


def test_environment_overrides_the_file(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Environment beats settings.yml (docs/architecture.md §12.3)."""
    (config_dir / "settings.yml").write_text("max_workers: 2\ngit:\n  branch: main\n", "utf-8")
    monkeypatch.setenv("OV_MAX_WORKERS", "9")
    monkeypatch.setenv("OV_GIT_BRANCH", "versioning")
    settings = _loader(config_dir).load().settings
    assert settings.max_workers == 9
    assert settings.git.branch == "versioning"
    assert settings.git.remote == "origin"  # untouched default


def test_invalid_environment_override_is_fatal(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed override is a configuration error, not a silent fallback."""
    monkeypatch.setenv("OV_MAX_WORKERS", "lots")
    with pytest.raises(ConfigurationError, match="expected an integer"):
        _loader(config_dir).load()


def test_cli_allow_mass_delete_overrides_everything(config_dir: Path) -> None:
    """CLI beats environment and file (docs/architecture.md §12.3)."""
    (config_dir / "settings.yml").write_text(
        "reconciliation:\n  allow_mass_delete: false\n", encoding="utf-8"
    )
    config = _loader(config_dir).load(allow_mass_delete=True)
    assert config.settings.reconciliation.allow_mass_delete is True


def test_database_selection_filters_the_run(config_dir: Path) -> None:
    """`--database` restricts the run to the named databases."""
    (config_dir / "databases.yml").write_text(VALID_DATABASE, encoding="utf-8")
    config = _loader(config_dir).load(only_databases=["HRDB"])
    assert [db.name for db in config.databases] == ["HRDB"]


def test_unknown_database_selection_is_fatal(config_dir: Path) -> None:
    """Naming a database that is not configured is a configuration error."""
    (config_dir / "databases.yml").write_text(VALID_DATABASE, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not configured"):
        _loader(config_dir).load(only_databases=["NOPE"])


def test_dry_run_and_no_git_reach_the_configuration(config_dir: Path) -> None:
    """The suppression flags are carried on AppConfig, not read from globals."""
    config = _loader(config_dir).load(dry_run=True, no_git=True)
    assert config.dry_run is True
    assert config.no_git is True


def test_shipped_config_directory_loads(project_root: Path) -> None:
    """The config/ files committed to this repository are themselves valid."""
    config = ConfigLoader(project_root / "config", secret_provider=FakeSecretProvider()).load()
    assert config.databases == ()
    assert config.settings == Settings()
