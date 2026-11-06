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


class GeminiError(AlchemyError):
    """A Gemini API call failed. The message never contains the API key."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        status: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status  # HTTP status, or None for a network failure
        self.detail = detail or message  # short cause, used in fallback notices


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
