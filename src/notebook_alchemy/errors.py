"""Typed errors. Each one carries what failed and the next action for the user."""

from __future__ import annotations


class AlchemyError(Exception):
    """Base class: a failure the CLI can explain without a traceback."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(AlchemyError):
    """An environment variable or setting is invalid."""

    exit_code = 2


class ChannelScanError(AlchemyError):
    """A channel, playlist or video URL could not be resolved to videos."""


class ClaudeError(AlchemyError):
    """A Claude API call failed or its answer cannot be used. The message never contains the key."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        status: int | None = None,
        detail: str | None = None,
        stop_run: bool = False,
        unusable: bool = False,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status  # HTTP status, or None for a network failure
        self.detail = detail or message  # short cause, used in notices
        # The key, the account's credit or spend limit, or the rate limit stops every request that
        # follows: the run stops, keeps what it has, and the next run continues.
        self.stop_run = stop_run
        # An answer arrived but cannot be used (refused, cut off): it was billed, so it is
        # remembered like a usable one instead of being asked again.
        self.unusable = unusable


class NotebookLMError(AlchemyError):
    """The `notebooklm` CLI is missing, failed, timed out or returned unusable output."""


class SessionError(NotebookLMError):
    """There is no valid NotebookLM session."""


class SyncError(NotebookLMError):
    """One or more compendiums could not be uploaded."""

    def __init__(self, message: str, *, failed: list[str], hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.failed = failed


class VaultError(AlchemyError):
    """The local vault could not be read or written."""
