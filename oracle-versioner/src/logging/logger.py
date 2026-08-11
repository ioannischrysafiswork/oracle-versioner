"""Logger configuration and secret redaction (docs/architecture.md §13).

Redaction is a filter on the *root* logger, applied to the formatted message
and to exception tracebacks, so no call site can leak a credential by
forgetting to mask it (CLAUDE.md §5.4).

Records carry ``timestamp`` (UTC, ISO-8601), ``level``, ``logger``,
``message``, ``execution_id``, and ``database`` where applicable. JSON is the
default in CI so log aggregation can parse it; text is more readable locally.

Use ``extra={"database": name}`` to attach a database to a record.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "MIN_SECRET_LENGTH", "REDACTED", "ContextFilter", "JsonFormatter",
    "RedactionFilter", "configure_logging", "get_logger", "load_logging_config",
    "register_secret",
]

REDACTED = "***REDACTED***"

#: Values shorter than this are never registered for redaction: a two-character
#: secret matches inside ordinary words, turning "other databases" into
#: "othe***REDACTED*** databases" and destroying the log while protecting
#: nothing. GitLab's masked variables use the same rule.
MIN_SECRET_LENGTH = 8

_TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime", "message", "taskName"
}


class RedactionFilter(logging.Filter):
    """Replaces known secret values wherever they appear in a log record.

    Secrets are registered as soon as they are resolved, before any connection
    is attempted. The filter rewrites the formatted message and the cached
    exception text, which is where a traceback's repr of a credential would
    otherwise surface.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        """Create the filter.

        Args:
            secrets: Initial secret values to redact.
        """
        super().__init__()
        self._secrets: set[str] = set()
        self.rejected_short = 0
        for secret in secrets:
            self.add_secret(secret)

    def add_secret(self, secret: str | None) -> None:
        """Register a value that must never appear in log output.

        Args:
            secret: The secret value. ``None``, empty strings, and values
                shorter than :data:`MIN_SECRET_LENGTH` are ignored — see that
                constant for why redacting a short value is worse than not
                redacting it. The rejection is logged, without the value.
        """
        if not secret:
            return
        if len(secret) < MIN_SECRET_LENGTH:
            self.rejected_short += 1
            logging.getLogger(__name__).warning(
                "a configured secret is shorter than %d characters and will NOT be "
                "redacted from logs; masking it would corrupt every record.",
                MIN_SECRET_LENGTH,
            )
            return
        self._secrets.add(secret)

    def redact(self, text: str) -> str:
        """Return ``text`` with every registered secret replaced.

        Longer secrets are replaced first, so a secret that contains another
        cannot leave a fragment behind.

        Args:
            text: Arbitrary text destined for a log sink.

        Returns:
            The redacted text.
        """
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record in place and always allow it through.

        Args:
            record: The record about to be emitted.

        Returns:
            ``True``, always — this filter censors, it never drops.
        """
        if not self._secrets:
            return True

        record.msg = self.redact(record.getMessage())
        record.args = ()

        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.exc_info:
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = self.redact(formatted)

        return True


class ContextFilter(logging.Filter):
    """Stamps the execution id onto every record."""

    def __init__(self, execution_id: str | None = None) -> None:
        """Create the filter.

        Args:
            execution_id: Correlation id for this execution.
        """
        super().__init__()
        self.execution_id = execution_id or "-"

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach ``execution_id`` and a default ``database``.

        Args:
            record: The record about to be emitted.

        Returns:
            ``True``, always.
        """
        if not hasattr(record, "execution_id"):
            record.execution_id = self.execution_id
        if not hasattr(record, "database"):
            record.database = None
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the standard fields plus any extras."""

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a single-line JSON object.

        Args:
            record: The record to render.

        Returns:
            The serialized record.
        """
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "execution_id": getattr(record, "execution_id", "-"),
            "database": getattr(record, "database", None),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


def load_logging_config(path: Path) -> dict[str, Any]:
    """Read ``logging.yml``, falling back to defaults when it is absent.

    A logging misconfiguration must never abort an execution, so a malformed
    file degrades to the defaults with a warning on stderr rather than raising.

    Args:
        path: Path to ``logging.yml``.

    Returns:
        The parsed mapping, or an empty mapping.
    """
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        # The logger is not configured yet -- stderr is the only sink there is.
        print(f"warning: ignoring unreadable {path}: {exc}", file=sys.stderr)  # noqa: T201
        return {}
    return data if isinstance(data, dict) else {}


_redaction_filter = RedactionFilter()


def configure_logging(
    level: str | None = None,
    *,
    execution_id: str | None = None,
    config_path: Path | None = None,
) -> RedactionFilter:
    """Install the root logger configuration for this execution.

    Args:
        level: Log level name. Overrides ``logging.yml``; defaults to its
            ``level``, or ``INFO``.
        execution_id: Correlation id attached to every record.
        config_path: Path to ``logging.yml``. Defaults to ``config/logging.yml``.

    Returns:
        The installed :class:`RedactionFilter`, so resolved secrets can be
        registered with it as they become known.

    Raises:
        ValueError: If ``level`` is not a valid level name.
    """
    config = load_logging_config(config_path or Path("config") / "logging.yml")
    resolved_level = (level or config.get("level") or "INFO").upper()
    use_json = str(config.get("format", "text")).lower() == "json"
    sinks = config.get("sinks") or {}

    root = logging.getLogger()
    root.setLevel(resolved_level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    for existing_filter in list(root.filters):
        root.removeFilter(existing_filter)

    context = ContextFilter(execution_id)
    formatter: logging.Formatter = (
        JsonFormatter() if use_json else logging.Formatter(_TEXT_FORMAT, datefmt=_DATE_FORMAT)
    )

    handlers: list[logging.Handler] = []
    if (sinks.get("stdout") or {}).get("enabled", True):
        handlers.append(logging.StreamHandler(stream=sys.stdout))

    file_sink = sinks.get("file") or {}
    if file_sink.get("enabled", False):
        target = Path(file_sink.get("path", "logs/oracle-versioner.log"))
        target.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            target,
            maxBytes=int(file_sink.get("max_bytes", 10 * 1024 * 1024)),
            backupCount=int(file_sink.get("backup_count", 3)),
            encoding="utf-8",
        ))

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(context)
        handler.addFilter(_redaction_filter)
        root.addHandler(handler)

    root.addFilter(context)
    root.addFilter(_redaction_filter)
    return _redaction_filter


def register_secret(secret: str | None) -> None:
    """Register a value with the process-wide redaction filter.

    A module-level entry point so code that discovers a secret late — the Git
    token, resolved at push time — can protect it without threading a callable
    through three layers.

    Args:
        secret: The value to mask; see :data:`MIN_SECRET_LENGTH`.
    """
    _redaction_filter.add_secret(secret)


def get_logger(name: str) -> logging.Logger:
    """Return the project logger for ``name``.

    Args:
        name: Usually ``__name__`` of the calling module.

    Returns:
        A standard library logger.
    """
    return logging.getLogger(name)
