"""Push with rebase-and-retry, and failure classification.

See docs/architecture.md §11.6 and §15.

  * **Non-fast-forward** (a concurrent pipeline pushed first): fetch, rebase,
    retry — up to the configured budget. This resolves the common case
    automatically.
  * **Auth / permission failure:** never retried. A revoked or wrongly scoped
    token fails identically every time and needs a human.
  * **Network failure:** retried within the same budget.

The token is injected into the remote URL **at call time** and passed as a
command argument. It is never written to ``.git/config``, so it cannot survive
into a CI artifact, and it is registered with the log redaction filter before
the first push so that any message quoting the URL is masked.
"""

from __future__ import annotations

import re

from src.config.models import GitSettings, RetrySettings
from src.config.secrets import EnvironmentSecretProvider, SecretProvider
from src.exceptions import PushError
from src.git.git_client import GitClient, redact_url
from src.logging.logger import get_logger, register_secret
from src.utils.retry import RetryPolicy, retry_call

__all__ = ["Pusher", "is_retryable_push_failure"]

_log = get_logger(__name__)

#: A concurrent pipeline pushed first, or the network blipped. Worth retrying.
_RETRYABLE = re.compile(
    r"non-fast-forward|fetch first|failed to push some refs|"
    r"could not resolve host|connection timed out|connection reset|"
    r"unable to access|early eof|rpc failed|the remote end hung up",
    re.IGNORECASE,
)

#: A credential problem. Deterministic — retrying only multiplies log noise.
_AUTHENTICATION = re.compile(
    r"authentication failed|could not read username|could not read password|"
    r"permission denied|access denied|403 forbidden|401 unauthorized|"
    r"repository not found|invalid username or password",
    re.IGNORECASE,
)


def is_retryable_push_failure(exc: BaseException) -> bool:
    """Whether a push failure is worth retrying.

    Authentication is checked first: a 403 while fetching can also mention
    "unable to access", and a credential problem must never be retried.

    Args:
        exc: The exception raised by the push.

    Returns:
        ``True`` for a non-fast-forward or a network fault.
    """
    message = str(exc)
    if _AUTHENTICATION.search(message):
        return False
    return bool(_RETRYABLE.search(message))


class Pusher:
    """Pushes the execution's commit to the configured branch."""

    def __init__(
        self,
        client: GitClient,
        git_settings: GitSettings,
        retry_settings: RetrySettings | None = None,
        secret_provider: SecretProvider | None = None,
    ) -> None:
        """Create a pusher.

        Args:
            client: The Git client for this repository.
            git_settings: Branch, remote, and the token's reference name.
            retry_settings: Attempt count and backoff.
            secret_provider: Resolves the token reference at call time.
        """
        self.client = client
        self.git = git_settings
        self.retry = retry_settings or RetrySettings()
        self.secret_provider: SecretProvider = secret_provider or EnvironmentSecretProvider()
        self._remote_url: str | None = None

    def remote_url(self) -> str:
        """Build the push URL, injecting the token at call time.

        The resolved token is registered for log redaction before it is used,
        so it cannot appear in output even via an unexpected Git error.

        Returns:
            The remote URL. When no token is configured the stored remote is
            used unchanged, which is what local runs against a filesystem
            remote need.

        Raises:
            PushError: If the remote is not configured.
        """
        if self._remote_url is not None:
            return self._remote_url

        stored = self.client.run(["remote", "get-url", self.git.remote], check=False)
        if not stored:
            raise PushError(f"remote {self.git.remote!r} is not configured")

        token = None
        try:
            token = self.secret_provider.resolve(self.git.token_env)
        except Exception:
            _log.debug("no push token configured; using the stored remote as-is")

        if token and stored.startswith(("http://", "https://")):
            register_secret(token)
            scheme, _, rest = stored.partition("://")
            rest = rest.split("@", 1)[-1]
            self._remote_url = f"{scheme}://oauth2:{token}@{rest}"
        else:
            self._remote_url = stored
        return self._remote_url

    def push(self, *, replacing: str | None = None) -> str:
        """Push HEAD to the configured branch, rebasing and retrying if needed.

        Args:
            replacing: The SHA this push is allowed to overwrite — the commit
                we ourselves pushed a moment ago. Supplying it turns the push
                into ``--force-with-lease=<branch>:<sha>`` (decision D-3).

                The lease is given an **explicit** expected value rather than
                relying on the bare ``--force-with-lease``: that form consults
                the remote-tracking ref, and pushing by URL never updates one,
                so it would fail with "stale info" every single time. The
                explicit form is also the tighter guarantee — it names exactly
                the commit we are willing to replace.

                A lease push is never retried. If the lease is refused, someone
                pushed after us, and rebasing to try again is precisely the
                clobber the lease exists to prevent.

        Returns:
            The SHA that was pushed.

        Raises:
            PushError: After the retry budget is exhausted, immediately on an
                authentication failure, or if the lease is refused. State is
                not advanced in any of those cases.
        """
        leased = replacing is not None
        policy = RetryPolicy(
            attempts=1 if leased else self.retry.push_attempts,
            base_delay=self.retry.push_backoff_seconds,
            jitter_ratio=self.retry.jitter_ratio,
        )

        def attempt() -> str:
            args = ["push"]
            if leased:
                args.append(f"--force-with-lease=refs/heads/{self.git.branch}:{replacing}")
            args += [self.remote_url(), f"HEAD:refs/heads/{self.git.branch}"]
            self.client.run(args)
            return self.client.rev_parse("HEAD")

        try:
            return retry_call(
                attempt,
                policy=policy,
                is_retryable=(lambda _exc: False) if leased else is_retryable_push_failure,
                description="push",
                on_retry=self._rebase_before_retry,
            )
        except PushError:
            raise
        except Exception as exc:
            raise PushError(f"push failed: {redact_url(str(exc))}") from exc

    def _rebase_before_retry(self, attempt: int, delay: float, exc: Exception) -> None:
        """Fetch and rebase between push attempts.

        A non-fast-forward means a concurrent pipeline pushed first; rebasing
        replays our single commit on top of theirs, which is exactly the
        recovery §11.6 describes.

        Args:
            attempt: The attempt that just failed, 1-based.
            delay: Seconds before the next attempt.
            exc: The failure.
        """
        _log.warning(
            "push attempt %d failed (%s); fetching, rebasing, retrying in %.1fs",
            attempt, redact_url(str(exc)), delay,
        )
        try:
            self.client.run(["fetch", self.remote_url(), self.git.branch])
            self.client.run(["rebase", "FETCH_HEAD"])
        except Exception as rebase_error:
            _log.warning("rebase failed: %s", redact_url(str(rebase_error)))
            self.client.run(["rebase", "--abort"], check=False)
