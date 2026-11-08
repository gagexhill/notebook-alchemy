"""Configuration management for Notebook Alchemy."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

logger = logging.getLogger(__name__)

# Default model: Google's most capable Flash model. Cheaper choices (gemini-3.5-flash-lite,
# gemini-2.5-flash-lite) are one ALCHEMY_GEMINI_MODEL setting away; gemini-2.5-flash shuts down
# on 2026-12-01.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
# Used when the default is overloaded or unavailable; set ALCHEMY_GEMINI_FALLBACK_MODEL to an empty
# value to turn the fallback off.
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"
DEFAULT_NOTEBOOKLM_PROFILE = "default"
# notebooklm-py 0.8.6 accepts ^[a-zA-Z0-9][a-zA-Z0-9_-]*$; the name is also a folder, so Windows
# device names are refused too.
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(1, 10)}
)
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_MAX_PER_VOLUME = 50
MAX_ALLOWED_CONCURRENCY = 16
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


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
        lines = path.read_text(encoding="utf-8-sig").splitlines()  # -sig: tolerate a BOM
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


def validate_profile_name(name: str, *, source: str = "ALCHEMY_NOTEBOOKLM_PROFILE") -> str:
    """A notebooklm profile name: letters, digits, `_` and `-`, starting with a letter or digit."""
    value = name.strip()
    if not _PROFILE_NAME.match(value) or value.lower() in WINDOWS_RESERVED_NAMES:
        where = " (in .env or your environment)" if source.isupper() else ""
        raise ConfigError(
            f"{source}={name!r}{where} is not a usable NotebookLM profile name.",
            hint="Use letters, digits, '_' or '-', and not a device name like nul (e.g. personal).",
        )
    return value


def update_dotenv(values: dict[str, str], env_path: Path | None = None) -> Path:
    """Set KEY=VALUE lines in a .env file, keeping every other line, and swap the file in whole.

    Existing assignments of the same keys are replaced in place; new keys are appended. Values are
    never logged. Returns the path written.
    """
    path = env_path or Path(".env")
    for key, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ConfigError(f"{key!r} is not an environment variable name.")
        if any(ch in value for ch in '\r\n"') or value != value.strip():
            raise ConfigError(
                f"The value for {key} contains a quote, a line break or surrounding spaces.",
                hint="Enter the value without those characters.",
            )
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"Could not read {path}: {exc}", hint="Fix or remove the file, then run setup again."
        ) from exc

    pending = dict(values)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        body = stripped[len("export ") :].lstrip() if stripped.startswith("export ") else stripped
        key = body.split("=", 1)[0].strip() if "=" in body and not body.startswith("#") else ""
        if key in values:
            if key in pending:
                out.append(_dotenv_line(key, pending.pop(key)))
            continue  # a later duplicate of a key already written
        out.append(raw)
    out.extend(_dotenv_line(k, v) for k, v in pending.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(out) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        raise ConfigError(f"Could not write {path}: {exc}") from exc
    return path


def _dotenv_line(key: str, value: str) -> str:
    return f'{key}="{value}"' if (" " in value or "#" in value) else f"{key}={value}"


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
    vault_path: Path = field(default_factory=lambda: Path("./vault").resolve())
    session_file: Path | None = None
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".notebook-alchemy" / "cache")
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    notebooklm_profile: str = DEFAULT_NOTEBOOKLM_PROFILE
    caption_languages: tuple[str, ...] = ("en", "en-US", "en-GB")

    @property
    def has_gemini_key(self) -> bool:
        return is_usable_api_key(self.gemini_api_key)

    @classmethod
    def from_env(cls) -> AlchemyConfig:
        """Create configuration from environment variables and a local .env."""
        _load_dotenv_simple()

        level = os.getenv("ALCHEMY_LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if level not in LOG_LEVELS:
            raise ConfigError(
                f"ALCHEMY_LOG_LEVEL={level!r} is not a log level.",
                hint=f"Use one of: {', '.join(LOG_LEVELS)}.",
            )

        def _path(name: str, default: Path | None) -> Path | None:
            value = os.getenv(name)
            return Path(value).expanduser().resolve() if value else default

        return cls(
            env=os.getenv("ALCHEMY_ENV", "development"),
            vault_path=_path("ALCHEMY_VAULT_PATH", Path("./vault").resolve()),  # type: ignore[arg-type]
            session_file=_path("ALCHEMY_SESSION_FILE", None),
            cache_dir=_path("ALCHEMY_CACHE_DIR", Path.home() / ".notebook-alchemy" / "cache"),  # type: ignore[arg-type]
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("ALCHEMY_GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL,
            gemini_fallback_model=os.getenv(
                "ALCHEMY_GEMINI_FALLBACK_MODEL", DEFAULT_GEMINI_FALLBACK_MODEL
            ).strip(),
            max_concurrency=_int_env(
                "ALCHEMY_MAX_CONCURRENCY",
                DEFAULT_MAX_CONCURRENCY,
                low=1,
                high=MAX_ALLOWED_CONCURRENCY,
            ),
            notebooklm_profile=validate_profile_name(
                os.getenv("ALCHEMY_NOTEBOOKLM_PROFILE", "").strip() or DEFAULT_NOTEBOOKLM_PROFILE
            ),
            caption_languages=tuple(
                code.strip()
                for code in os.getenv("ALCHEMY_CAPTION_LANGUAGES", "en,en-US,en-GB").split(",")
                if code.strip()
            )
            or ("en",),
        )
