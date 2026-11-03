"""Configuration management for Notebook Alchemy."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AlchemyConfig:
    """Runtime configuration for the Alchemy engine."""

    env: str = "development"
    log_level: str = "INFO"
    vault_path: Path = field(default_factory=lambda: Path("./vault").resolve())
    state_dir: Path = field(default_factory=lambda: Path("./.notebooklm").resolve())
    session_file: Path | None = None

    @classmethod
    def from_env(cls) -> AlchemyConfig:
        """Create configuration instance from environment variables."""
        vault_env = os.getenv("ALCHEMY_VAULT_PATH")
        state_env = os.getenv("ALCHEMY_STATE_DIR")
        session_env = os.getenv("ALCHEMY_SESSION_FILE")

        return cls(
            env=os.getenv("ALCHEMY_ENV", "development"),
            log_level=os.getenv("ALCHEMY_LOG_LEVEL", "INFO"),
            vault_path=Path(vault_env).resolve() if vault_env else Path("./vault").resolve(),
            state_dir=Path(state_env).resolve() if state_env else Path("./.notebooklm").resolve(),
            session_file=Path(session_env).resolve() if session_env else None,
        )

    def ensure_directories(self) -> None:
        """Ensure necessary runtime directories exist locally."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
