"""Markdown vault management and synchronization."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import AlchemyConfig

logger = logging.getLogger(__name__)


class VaultManager:
    """Manages reading and writing structured markdown artifacts in the local vault."""

    def __init__(self, config: AlchemyConfig) -> None:
        self.config = config
        self.vault_path = config.vault_path

    def ensure_vault(self) -> Path:
        """Ensure vault path exists."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        return self.vault_path

    def write_note(
        self,
        relative_path: str,
        content: str,
        frontmatter: dict[str, Any] | None = None,
    ) -> Path:
        """Write a synthesized markdown note to the vault with optional YAML frontmatter."""
        self.ensure_vault()
        target_path = self.vault_path / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        full_content = ""
        if frontmatter:
            yaml_lines = ["---"]
            for key, val in frontmatter.items():
                yaml_lines.append(f"{key}: {val}")
            yaml_lines.append("---\n")
            full_content = "\n".join(yaml_lines)

        full_content += content

        target_path.write_text(full_content, encoding="utf-8")
        logger.info("Saved note to vault: %s", target_path)
        return target_path

    def list_notes(self) -> list[Path]:
        """List all markdown files within the vault."""
        if not self.vault_path.exists():
            return []
        return sorted(self.vault_path.glob("**/*.md"))
