"""Configuration management for Notebook Alchemy."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

logger = logging.getLogger(__name__)

# Default model: Google's most capable Flash model. Cheaper choices (gemini-3.5-flash-lite,
# gemini-2.5-flash-lite) are one ALCHEMY_GEMINI_MODEL setting away; gemini-2.5-flash shuts down
# on 2026-12-01.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_MAX_CONCURRENCY = 4
MAX_ALLOWED_CONCURRENCY = 16
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def is_usable_api_key(key: str | None) -> bool:
    """A real-looking key: non-empty, not a placeholder, at least 15 characters."""
    value = (key or "").strip()
    if not value or value.startswith("your_"):
        return False
    return len(value) >= 15


def _load_dotenv_simple(env_path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ without overriding real variables."""
    path = env_path or Path(".env")
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read %s (%s); using process environment only.", path, exc)
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ[key] = value


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a whole number, got {raw!r}.",
            hint=f"Set {name} to a value between {low} and {high}, or remove it.",
        ) from exc
    if not low <= value <= high:
        raise ConfigError(
            f"{name}={value} is outside {low}-{high}.",
            hint=f"Set {name} to a value between {low} and {high}.",
        )
    return value


@dataclass
class AlchemyConfig:
    """Runtime configuration for the Alchemy engine."""

    env: str = "development"
    log_level: str = "INFO"
    vault_path: Path = field(default_factory=lambda: Path("./vault").resolve())
    state_dir: Path = field(default_factory=lambda: Path("./.notebooklm").resolve())
    session_file: Path | None = None
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".notebook-alchemy" / "cache")
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = DEFAULT_GEMINI_MODEL
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY

    @property
    def has_gemini_key(self) -> bool:
        return is_usable_api_key(self.gemini_api_key)

    @classmethod
    def from_env(cls) -> AlchemyConfig:
        """Create configuration from environment variables and a local .env."""
        _load_dotenv_simple()

        level = os.getenv("ALCHEMY_LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if level not in _LOG_LEVELS:
            raise ConfigError(
                f"ALCHEMY_LOG_LEVEL={level!r} is not a log level.",
                hint=f"Use one of: {', '.join(_LOG_LEVELS)}.",
            )

        def _path(name: str, default: Path | None) -> Path | None:
            value = os.getenv(name)
            return Path(value).expanduser().resolve() if value else default

        return cls(
            env=os.getenv("ALCHEMY_ENV", "development"),
            log_level=level,
            vault_path=_path("ALCHEMY_VAULT_PATH", Path("./vault").resolve()),  # type: ignore[arg-type]
            state_dir=_path("ALCHEMY_STATE_DIR", Path("./.notebooklm").resolve()),  # type: ignore[arg-type]
            session_file=_path("ALCHEMY_SESSION_FILE", None),
            cache_dir=_path("ALCHEMY_CACHE_DIR", Path.home() / ".notebook-alchemy" / "cache"),  # type: ignore[arg-type]
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("ALCHEMY_GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL,
            max_concurrency=_int_env(
                "ALCHEMY_MAX_CONCURRENCY",
                DEFAULT_MAX_CONCURRENCY,
                low=1,
                high=MAX_ALLOWED_CONCURRENCY,
            ),
        )
