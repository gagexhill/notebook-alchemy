"""Configuration management for Notebook Alchemy."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv_simple(env_path: Path | None = None) -> None:
    """Load key-value pairs from a .env file into os.environ if not already set."""
    path = env_path or Path(".env")
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            # Don't overwrite existing environment variables
            if k and k not in os.environ:
                # Strip quotes if present
                if (v.startswith('"') and v.endswith('"')) or (
                    v.startswith("'") and v.endswith("'")
                ):
                    v = v[1:-1]
                os.environ[k] = v
    except Exception:  # noqa: BLE001, S110
        pass


@dataclass
class AlchemyConfig:
    """Runtime configuration for the Alchemy engine."""

    env: str = "development"
    log_level: str = "INFO"
    vault_path: Path = field(default_factory=lambda: Path("./vault").resolve())
    state_dir: Path = field(default_factory=lambda: Path("./.notebooklm").resolve())
    session_file: Path | None = None
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))

    @classmethod
    def from_env(cls) -> AlchemyConfig:
        """Create configuration instance from environment variables and local .env."""
        _load_dotenv_simple()

        vault_env = os.getenv("ALCHEMY_VAULT_PATH")
        state_env = os.getenv("ALCHEMY_STATE_DIR")
        session_env = os.getenv("ALCHEMY_SESSION_FILE")

        return cls(
            env=os.getenv("ALCHEMY_ENV", "development"),
            log_level=os.getenv("ALCHEMY_LOG_LEVEL", "INFO"),
            vault_path=Path(vault_env).resolve() if vault_env else Path("./vault").resolve(),
            state_dir=Path(state_env).resolve() if state_env else Path("./.notebooklm").resolve(),
            session_file=Path(session_env).resolve() if session_env else None,
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        )

    def ensure_directories(self) -> None:
        """Ensure necessary runtime directories exist locally."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
