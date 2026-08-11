"""Extraction dispatcher: object type to strategy (docs/architecture.md §5.1).

The only place that decides which extraction mechanism is used, so supporting a
new type means one table entry plus one strategy. It also owns the pre-write
validation gate (§5.5): a failed extraction must never overwrite a good
repository file with empty or partial content.

Two empty results are distinguished, and the difference matters:

  * **No rows at all** — the object was dropped between discovery and
    extraction. Not a failure: logged at ``WARNING``, skipped, and removed by
    reconciliation (§22.4). :class:`ObjectVanished` signals this.
  * **Rows returned, but the text is blank or malformed** — a real extraction
    failure. ``FAILED_EXTRACTION``; the existing file is left untouched.
"""

from __future__ import annotations

from typing import Protocol

from src.config.models import Settings
from src.exceptions import ExtractionError
from src.logging.logger import get_logger
from src.models.oracle_object import ExtractedSource, ObjectType, OracleObject
from src.oracle.connection import Session, is_transient
from src.oracle.ddl_extractor import MetadataStrategy
from src.oracle.source_extractor import (
    ColumnStrategy,
    DictionaryStrategy,
    SourceStrategy,
    ViewStrategy,
)
from src.utils.retry import RetryPolicy, retry_call

__all__ = ["STRATEGY_BY_TYPE", "ExtractionDispatcher", "ExtractionStrategy", "ObjectVanished"]

_log = get_logger(__name__)


class ExtractionStrategy(Protocol):
    """What every extraction strategy must provide."""

    def __init__(self, session: Session) -> None:
        """Bind the strategy to a session."""
        ...

    def extract(self, obj: OracleObject) -> ExtractedSource:
        """Extract ``obj``'s source. Empty text means the object has vanished."""
        ...


#: Object type to strategy class (docs/architecture.md §5.1). VIEW uses
#: ``DBA_VIEWS`` rather than ``DBA_SOURCE``; see :mod:`src.oracle.source_extractor`.
STRATEGY_BY_TYPE: dict[ObjectType, type[ExtractionStrategy]] = {
    ObjectType.PACKAGE: SourceStrategy,
    ObjectType.PACKAGE_BODY: SourceStrategy,
    ObjectType.PROCEDURE: SourceStrategy,
    ObjectType.FUNCTION: SourceStrategy,
    ObjectType.TYPE: SourceStrategy,
    ObjectType.TYPE_BODY: SourceStrategy,
    ObjectType.TRIGGER: SourceStrategy,
    ObjectType.VIEW: ViewStrategy,
    ObjectType.TABLE: ColumnStrategy,
    ObjectType.SEQUENCE: MetadataStrategy,
    ObjectType.SYNONYM: DictionaryStrategy,
    ObjectType.MATERIALIZED_VIEW: MetadataStrategy,
}

#: The leading construct each type's extracted source must contain.
_EXPECTED_LEADING: dict[ObjectType, str] = {
    ObjectType.TABLE: "CREATE TABLE",
    ObjectType.SEQUENCE: "CREATE SEQUENCE",
    ObjectType.MATERIALIZED_VIEW: "CREATE MATERIALIZED VIEW",
    ObjectType.SYNONYM: "SYNONYM",
    ObjectType.VIEW: "CREATE OR REPLACE VIEW",
}


class ObjectVanished(Exception):  # noqa: N818 - not a failure; see below
    """The object disappeared between discovery and extraction.

    Not an error condition: the run unit logs a warning, skips the object, and
    lets reconciliation remove its file (docs/architecture.md §22.4).
    """


class ExtractionDispatcher:
    """Routes an object to its extraction strategy and validates the result."""

    def __init__(self, session: Session, settings: Settings) -> None:
        """Create a dispatcher.

        Args:
            session: An initialized read-only session.
            settings: Global settings, supplying the retry budget.
        """
        self.session = session
        self.settings = settings
        self._strategies: dict[type, ExtractionStrategy] = {}

    def extract(self, obj: OracleObject) -> ExtractedSource:
        """Extract ``obj`` using the strategy registered for its type.

        A connectivity-class failure is retried once; a logical failure — a
        missing privilege, wrapped source, malformed output — is deterministic
        and never retried (docs/architecture.md §15).

        Args:
            obj: The object to extract.

        Returns:
            The validated raw source.

        Raises:
            ObjectVanished: If the object no longer exists.
            ExtractionError: If extraction or validation fails.
        """
        strategy = self._strategy_for(obj.object_type)
        retry = self.settings.retry
        policy = RetryPolicy(
            attempts=retry.extraction_attempts + 1,
            base_delay=retry.connect_backoff_seconds,
            jitter_ratio=retry.jitter_ratio,
        )

        try:
            source = retry_call(
                lambda: strategy.extract(obj),
                policy=policy,
                is_retryable=is_transient,
                description=f"extract {obj.owner}.{obj.object_name}",
                on_retry=lambda attempt, delay, exc: _log.warning(
                    "extraction attempt %d for %s.%s failed (%s); retrying in %.1fs",
                    attempt, obj.owner, obj.object_name, exc, delay,
                    extra={"database": obj.database},
                ),
            )
        except Exception as exc:
            raise ExtractionError(
                f"{obj.owner}.{obj.object_name} ({obj.object_type}): extraction failed: {exc}"
            ) from exc

        self.validate(source)
        return source

    def _strategy_for(self, object_type: ObjectType) -> ExtractionStrategy:
        """Return the cached strategy instance for ``object_type``.

        Raises:
            ExtractionError: If the type has no registered strategy.
        """
        try:
            strategy_class = STRATEGY_BY_TYPE[object_type]
        except KeyError as exc:
            raise ExtractionError(f"no extraction strategy for {object_type}") from exc

        if strategy_class not in self._strategies:
            self._strategies[strategy_class] = strategy_class(self.session)
        return self._strategies[strategy_class]

    @staticmethod
    def validate(source: ExtractedSource) -> None:
        """Reject empty, whitespace-only, or structurally wrong source.

        This is the gate that stops a failed extraction from overwriting a good
        file with garbage (docs/architecture.md §5.5).

        Args:
            source: The extracted source to check.

        Raises:
            ObjectVanished: If nothing at all was returned.
            ExtractionError: If the text is blank or lacks the expected leading
                construct for its type.
        """
        obj = source.obj
        identity = f"{obj.owner}.{obj.object_name} ({obj.object_type})"

        if not source.text:
            raise ObjectVanished(identity)
        if not source.text.strip():
            raise ExtractionError(f"{identity}: extracted source is whitespace only")

        expected = _EXPECTED_LEADING.get(obj.object_type)
        head = source.text.lstrip()[:200].upper()
        if expected and expected not in head:
            raise ExtractionError(f"{identity}: extracted source does not start with {expected!r}")
        if expected is None and not head.startswith("CREATE OR REPLACE"):
            raise ExtractionError(f"{identity}: extracted source is not a CREATE OR REPLACE")
