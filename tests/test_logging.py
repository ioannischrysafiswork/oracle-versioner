"""Logging: redaction, structured output, and context fields.

The redaction contract is blocking in CI (CLAUDE.md §6): a log stream produced
with a known sentinel password never contains that sentinel — in a message, in
an argument, or in a traceback.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.logging.logger import (
    REDACTED,
    ContextFilter,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    load_logging_config,
)
from tests.conftest import SENTINEL_PASSWORD


@pytest.fixture
def redacting_stream(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> RedactionFilter:
    """A configured root logger whose filter knows the sentinel."""
    redactor = configure_logging("DEBUG", config_path=tmp_path / "absent.yml")
    redactor.add_secret(SENTINEL_PASSWORD)
    caplog.handler.addFilter(redactor)
    caplog.set_level(logging.DEBUG)
    return redactor


# --------------------------------------------------------------------------- redaction


def test_sentinel_never_appears_in_a_message(
    redacting_stream: RedactionFilter, caplog: pytest.LogCaptureFixture
) -> None:
    """A password interpolated into a message is redacted."""
    logging.getLogger("t").info("connecting with password %s", SENTINEL_PASSWORD)
    assert SENTINEL_PASSWORD not in caplog.text
    assert REDACTED in caplog.text


def test_sentinel_never_appears_in_a_dsn(
    redacting_stream: RedactionFilter, caplog: pytest.LogCaptureFixture
) -> None:
    """A credential-bearing DSN is redacted."""
    logging.getLogger("t").error("dsn=oracle://user:%s@host:1521/svc", SENTINEL_PASSWORD)
    assert SENTINEL_PASSWORD not in caplog.text


def test_sentinel_never_appears_in_a_traceback(
    redacting_stream: RedactionFilter, caplog: pytest.LogCaptureFixture
) -> None:
    """A password embedded in an exception message is redacted."""
    try:
        raise ValueError(f"bad credential {SENTINEL_PASSWORD}")
    except ValueError:
        logging.getLogger("t").exception("connection failed")
    assert SENTINEL_PASSWORD not in caplog.text


def test_empty_secret_is_ignored() -> None:
    """Registering an empty secret must not corrupt every record."""
    redactor = RedactionFilter()
    redactor.add_secret("")
    redactor.add_secret(None)
    assert redactor.redact("unchanged") == "unchanged"


def test_short_secret_is_rejected_rather_than_mangling_every_record() -> None:
    """A one-character secret would match inside ordinary words.

    Regression: a single-character username turned "other databases" into
    "othe***REDACTED*** databases" and "duration" into "du***REDACTED***ation",
    destroying the log while protecting nothing.
    """
    redactor = RedactionFilter()
    redactor.add_secret("r")
    assert redactor.redact("other databases, duration=6.1s") == "other databases, duration=6.1s"
    assert redactor.rejected_short == 1


@pytest.mark.parametrize("secret", ["a", "ab", "abc1234"])
def test_secrets_below_the_threshold_are_not_registered(secret: str) -> None:
    """The threshold is applied consistently just below the boundary."""
    redactor = RedactionFilter()
    redactor.add_secret(secret)
    assert redactor.redact(f"value={secret}") == f"value={secret}"


def test_secrets_at_the_threshold_are_redacted() -> None:
    """A secret of the minimum length onwards is masked normally."""
    redactor = RedactionFilter()
    redactor.add_secret("abcd1234")
    assert "abcd1234" not in redactor.redact("value=abcd1234")


def test_short_secret_rejection_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The operator is told, without the value appearing in the message."""
    caplog.set_level(logging.WARNING)
    RedactionFilter().add_secret("xy")
    assert "shorter than" in caplog.text
    assert "xy" not in caplog.text.replace("proxy", "")


def test_overlapping_secrets_leave_no_fragment() -> None:
    """A secret containing another is replaced whole, longest first.

    Replacing the shorter one first would leave ``***REDACTED***ij`` — the tail
    of the longer secret exposed in the log.
    """
    redactor = RedactionFilter(["abcdefgh", "abcdefghij"])
    assert redactor.redact("token=abcdefghij") == f"token={REDACTED}"


# --------------------------------------------------------------------------- structure


def _render(record_kwargs: dict | None = None, message: str = "hello") -> dict:
    record = logging.LogRecord("src.test", logging.INFO, "f.py", 1, message, None, None)
    for key, value in (record_kwargs or {}).items():
        setattr(record, key, value)
    ContextFilter("exec-1").filter(record)
    return json.loads(JsonFormatter().format(record))


def test_json_records_carry_the_required_fields() -> None:
    """Every record has the fields docs/architecture.md §13.1 requires."""
    payload = _render()
    assert payload["level"] == "INFO"
    assert payload["logger"] == "src.test"
    assert payload["message"] == "hello"
    assert payload["execution_id"] == "exec-1"
    assert payload["database"] is None
    assert payload["timestamp"].endswith("+00:00")


def test_database_extra_is_carried_onto_the_record() -> None:
    """`extra={"database": ...}` reaches the structured output."""
    assert _render({"database": "HRDB"})["database"] == "HRDB"


def test_context_filter_does_not_overwrite_an_explicit_execution_id() -> None:
    """A record that already carries an id keeps it."""
    assert _render({"execution_id": "explicit"})["execution_id"] == "explicit"


def test_json_output_is_one_line() -> None:
    """Aggregators expect one object per line."""
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "multi\nline", None, None)
    ContextFilter("e").filter(record)
    assert "\n" not in JsonFormatter().format(record)


# --------------------------------------------------------------------------- configuration


def test_missing_logging_config_falls_back_to_defaults(tmp_path: Path) -> None:
    """logging.yml is optional."""
    assert load_logging_config(tmp_path / "absent.yml") == {}


def test_malformed_logging_config_degrades_instead_of_aborting(tmp_path: Path) -> None:
    """A logging misconfiguration must never abort an execution."""
    path = tmp_path / "logging.yml"
    path.write_text("level: [unclosed\n", encoding="utf-8")
    assert load_logging_config(path) == {}


def test_level_comes_from_the_file_when_no_override(tmp_path: Path) -> None:
    """logging.yml sets the level in the absence of a CLI argument."""
    path = tmp_path / "logging.yml"
    path.write_text("level: WARNING\n", encoding="utf-8")
    configure_logging(None, config_path=path)
    assert logging.getLogger().level == logging.WARNING


def test_explicit_level_overrides_the_file(tmp_path: Path) -> None:
    """The CLI wins over logging.yml (docs/architecture.md §12.3)."""
    path = tmp_path / "logging.yml"
    path.write_text("level: WARNING\n", encoding="utf-8")
    configure_logging("DEBUG", config_path=path)
    assert logging.getLogger().level == logging.DEBUG


def test_json_format_and_file_sink_are_honoured(tmp_path: Path) -> None:
    """The file sink writes JSON lines under the configured path."""
    log_file = tmp_path / "logs" / "agent.log"
    path = tmp_path / "logging.yml"
    path.write_text(
        f"level: INFO\nformat: json\nsinks:\n  stdout:\n    enabled: false\n"
        f"  file:\n    enabled: true\n    path: {log_file.as_posix()}\n",
        encoding="utf-8",
    )
    redactor = configure_logging(None, execution_id="exec-9", config_path=path)
    redactor.add_secret(SENTINEL_PASSWORD)
    logging.getLogger("t").info("token %s", SENTINEL_PASSWORD, extra={"database": "HRDB"})

    for handler in logging.getLogger().handlers:
        handler.flush()
    payload = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert payload["execution_id"] == "exec-9"
    assert payload["database"] == "HRDB"
    assert SENTINEL_PASSWORD not in log_file.read_text(encoding="utf-8")

    configure_logging("INFO", config_path=tmp_path / "absent.yml")  # release the file handle


def test_reconfiguring_does_not_duplicate_handlers(tmp_path: Path) -> None:
    """Repeated configuration must not emit each record twice."""
    for _ in range(3):
        configure_logging("INFO", config_path=tmp_path / "absent.yml")
    assert len(logging.getLogger().handlers) == 1


def test_shipped_logging_config_parses(project_root: Path) -> None:
    """The logging.yml committed to this repository is valid."""
    config = load_logging_config(project_root / "config" / "logging.yml")
    assert config["level"] == "INFO"
    assert config["sinks"]["file"]["enabled"] is False
