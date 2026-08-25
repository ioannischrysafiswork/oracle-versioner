"""Pure validation helpers for the YAML configuration.

Split out of :mod:`src.config.loader` so that loader stays orchestration (read
files, apply precedence, resolve secrets) and this module stays pure functions
over already-parsed data. Everything here is total and side-effect free apart
from reading a file in :func:`read_yaml`, which makes the validation rules
testable without touching :class:`~src.config.loader.ConfigLoader`.

**This module is not in the approved structure** (CLAUDE.md §3) — see the
Phase 2 report; it exists because ``loader.py`` otherwise exceeds the ~300-line
limit in §7.3, and it needs owner ratification.

Every failure raises :class:`~src.exceptions.ConfigurationError`, which is
execution-fatal with exit code 2.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from src.config.models import (
    DatabaseConfig,
    ObjectTypeFilter,
    SchemaFilter,
    Settings,
)
from src.exceptions import ConfigurationError
from src.models.oracle_object import ObjectType

__all__ = [
    "ENV_OVERRIDES",
    "REQUIRED_DATABASE_KEYS",
    "SECRET_KEYS",
    "apply_env_overrides",
    "build_database",
    "build_settings_section",
    "read_yaml",
]


#: Environment overrides, applied after the file and before CLI arguments.
#: Explicit rather than derived, so the supported surface stays auditable.
ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    "OV_MAX_WORKERS": ("max_workers", int),
    "OV_WATERMARK_OVERLAP_MINUTES": ("watermark_overlap_minutes", int),
    "OV_REPOSITORY_ROOT": ("repository_root", str),
    "OV_STATE_FILE": ("state_file", str),
    "OV_GIT_BRANCH": ("git.branch", str),
    "OV_GIT_REMOTE": ("git.remote", str),
}

#: Secret reference fields on a database entry. Names only, never values.
SECRET_KEYS = ("dsn_env", "username_env", "password_env")
REQUIRED_DATABASE_KEYS = ("name", "git_folder", *SECRET_KEYS)
_DATABASE_KEYS = {*REQUIRED_DATABASE_KEYS, "enabled", "thick_mode", "schemas", "object_types"}


def _fail(message: str) -> None:
    """Raise a configuration error."""
    raise ConfigurationError(message)


def read_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML mapping from ``path``.

    Args:
        path: The file to read.

    Returns:
        The parsed mapping; empty when the file is empty.

    Raises:
        ConfigurationError: If the file is missing, unreadable, not valid YAML,
            or does not hold a mapping at the top level.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path} is not valid YAML: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        _fail(f"{path}: expected a mapping at the top level")
    return data


def build_settings_section[T](cls: type[T], data: Any, context: str) -> T:
    """Populate a defaults-only settings dataclass from a mapping.

    Absent keys fall back to the built-in default, so only supplied keys are
    validated.

    Args:
        cls: A dataclass whose fields all have defaults.
        data: The raw mapping, or ``None`` for "use all defaults".
        context: Key path, used in error messages.

    Returns:
        The populated dataclass.

    Raises:
        ConfigurationError: On an unknown key or a wrongly typed value.
    """
    if data is None:
        return cls()
    if not isinstance(data, dict):
        _fail(f"{context}: expected a mapping, got {type(data).__name__}")

    defaults = cls()
    # Every caller passes a settings dataclass whose fields all have defaults —
    # that is the precondition this function is built on — but the constraint
    # is not expressible in the type system without depending on the private
    # `_typeshed.DataclassInstance` protocol.
    unknown = set(data) - {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    if unknown:
        _fail(f"{context}: unknown key(s) {sorted(unknown)}")

    values: dict[str, Any] = {}
    for key, value in data.items():
        expected = type(getattr(defaults, key))
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if isinstance(value, bool) != (expected is bool) or not isinstance(value, expected):
            _fail(f"{context}.{key}: expected {expected.__name__}, got {type(value).__name__}")
        values[key] = value
    return cls(**values)


def _string_list(data: Any, context: str) -> tuple[str, ...]:
    """Validate a list of non-empty strings, upper-cased."""
    raw = data or []
    if not isinstance(raw, list) or not all(isinstance(s, str) and s for s in raw):
        _fail(f"{context}: expected a list of non-empty strings")
    return tuple(s.upper() for s in raw)


def _git_folder(value: str, context: str) -> str:
    """Reject a git folder that is absolute or could escape the repository root.

    Deliberately not ``Path.is_absolute()``: on Windows that reports ``/etc``
    as relative, so the same configuration would validate differently on a
    developer machine and on the Linux CI runner.
    """
    folder = value.replace("\\", "/").strip("/")
    if not folder:
        _fail(f"{context}.git_folder: must not be empty")
    if value.startswith(("/", "\\")) or PureWindowsPath(value).drive or ".." in folder.split("/"):
        _fail(f"{context}.git_folder: must be a relative path inside the repository: {value!r}")
    return folder


def _schema_filter(data: Any, context: str) -> SchemaFilter:
    """Validate the include/exclude schema filter."""
    if data is None:
        return SchemaFilter()
    if not isinstance(data, dict):
        _fail(f"{context}.schemas: expected a mapping")
    unknown = set(data) - {"include", "exclude"}
    if unknown:
        _fail(f"{context}.schemas: unknown key(s) {sorted(unknown)}")

    include = _string_list(data.get("include"), f"{context}.schemas.include")
    exclude = _string_list(data.get("exclude"), f"{context}.schemas.exclude")
    both = sorted(set(include) & set(exclude))
    if both:
        _fail(f"{context}.schemas: {both} appear in both include and exclude")
    return SchemaFilter(include=include, exclude=exclude)


def _type_filter(data: Any, context: str) -> ObjectTypeFilter:
    """Validate the object type list; absent means all twelve supported types."""
    if data is None:
        return ObjectTypeFilter()
    if not isinstance(data, list) or not data:
        _fail(f"{context}.object_types: expected a non-empty list")

    types: list[ObjectType] = []
    for raw in data:
        if not isinstance(raw, str):
            _fail(f"{context}.object_types: expected strings")
        try:
            parsed = ObjectType(raw.upper())
        except ValueError as exc:
            supported = sorted(str(t) for t in ObjectType)
            raise ConfigurationError(
                f"{context}.object_types: unknown object type {raw!r}; supported: {supported}"
            ) from exc
        if parsed in types:
            _fail(f"{context}.object_types: duplicate {raw!r}")
        types.append(parsed)
    return ObjectTypeFilter(types=tuple(types))


def build_database(entry: Any, index: int) -> DatabaseConfig:
    """Validate one entry of the ``databases`` list.

    Args:
        entry: The raw mapping.
        index: Position in the list, used in error messages.

    Returns:
        The validated database configuration, carrying secret names only.

    Raises:
        ConfigurationError: On any structural or semantic error.
    """
    context = f"databases.yml: databases[{index}]"
    if not isinstance(entry, dict):
        _fail(f"{context}: expected a mapping")

    unknown = set(entry) - _DATABASE_KEYS
    if unknown:
        _fail(f"{context}: unknown key(s) {sorted(unknown)}")
    missing = sorted(set(REQUIRED_DATABASE_KEYS) - set(entry))
    if missing:
        _fail(f"{context}: missing required key(s) {missing}")

    for key in REQUIRED_DATABASE_KEYS:
        if not isinstance(entry[key], str) or not entry[key].strip():
            _fail(f"{context}.{key}: expected a non-empty string")
    for key in ("enabled", "thick_mode"):
        if key in entry and not isinstance(entry[key], bool):
            _fail(f"{context}.{key}: expected a boolean")

    return DatabaseConfig(
        name=entry["name"],
        git_folder=_git_folder(entry["git_folder"], context),
        dsn_env=entry["dsn_env"],
        username_env=entry["username_env"],
        password_env=entry["password_env"],
        enabled=entry.get("enabled", True),
        thick_mode=entry.get("thick_mode", False),
        schemas=_schema_filter(entry.get("schemas"), context),
        object_types=_type_filter(entry.get("object_types"), context),
    )


def apply_env_overrides(settings: Settings) -> Settings:
    """Apply the supported environment overrides to ``settings``.

    Args:
        settings: Settings as read from the file.

    Returns:
        The settings with overrides applied.

    Raises:
        ConfigurationError: If an override value cannot be converted.
    """
    top: dict[str, Any] = {}
    git: dict[str, Any] = {}
    for name, (target, target_type) in ENV_OVERRIDES.items():
        raw = os.environ.get(name)
        if raw is None:
            continue
        if target_type is int:
            try:
                value: Any = int(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{name}: expected an integer, got {raw!r}") from exc
        else:
            value = raw
        if target.startswith("git."):
            git[target.removeprefix("git.")] = value
        else:
            top[target] = value

    if git:
        top["git"] = dataclasses.replace(settings.git, **git)
    return dataclasses.replace(settings, **top) if top else settings
