"""Secret resolution (docs/architecture.md §12.2, §21.1).

Configuration files carry secret *names*; a ``SecretProvider`` turns a name
into a value at runtime. The rest of the application never learns which
provider is in use, so Vault or a cloud Secrets Manager can be substituted
without touching a single call site.

PHASE 2 SKELETON — the environment provider is the only implementation, and it
is deliberately trivial. No value returned from here may ever reach a log
record un-redacted (see :mod:`src.logging.logger`).
"""

from __future__ import annotations

import os
from typing import Protocol

from src.exceptions import ConfigurationError

__all__ = ["EnvironmentSecretProvider", "SecretProvider"]


class SecretProvider(Protocol):
    """Resolves a secret reference name to its value."""

    def resolve(self, name: str) -> str:
        """Return the secret registered under ``name``.

        Args:
            name: The reference name from configuration, e.g. ``HRDB_PASSWORD``.

        Returns:
            The secret value.

        Raises:
            ConfigurationError: If the reference cannot be resolved.
        """
        ...


class EnvironmentSecretProvider:
    """Default provider: resolves references from environment variables."""

    def resolve(self, name: str) -> str:
        """Return the value of environment variable ``name``.

        Args:
            name: The environment variable name from configuration.

        Returns:
            The secret value.

        Raises:
            ConfigurationError: If the variable is unset or empty. The message
                names the variable only — never a value.
        """
        value = os.environ.get(name)
        if not value:
            raise ConfigurationError(f"secret reference {name!r} is not set in the environment")
        return value
