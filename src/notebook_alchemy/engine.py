"""Core AlchemyEngine orchestrator."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .client import BaseNotebookClient, SourceDocument
from .config import AlchemyConfig
from .vault import VaultManager

logger = logging.getLogger(__name__)


@dataclass
class SynthesisResult:
    """Represents the output of a knowledge synthesis run."""

    artifact_id: str
    title: str
    markdown_content: str
    sources_referenced: list[str]
    metadata: dict[str, Any] | None = None


class AlchemyEngine:
    """Core knowledge orchestration and synthesis engine."""

    def __init__(self, config: AlchemyConfig | None = None) -> None:
        self.config = config or AlchemyConfig.from_env()
        self.client = BaseNotebookClient(self.config)
        self.vault = VaultManager(self.config)

    def status(self) -> dict[str, Any]:
        """Return engine operational status and configuration."""
        return {
            "environment": self.config.env,
            "authenticated": self.client.is_authenticated(),
            "vault_path": str(self.config.vault_path),
            "state_dir": str(self.config.state_dir),
            "sources_count": len(self.client.list_sources()),
        }

    def process_and_export(
        self,
        document: SourceDocument,
        relative_vault_path: str,
    ) -> SynthesisResult:
        """Ingest document, process, and persist note to local markdown vault."""
        source_id = self.client.ingest_source(document)

        frontmatter = {
            "title": f'"{document.title}"',
            "source_type": document.source_type,
            "source_id": source_id,
        }

        self.vault.write_note(
            relative_path=relative_vault_path,
            content=document.content,
            frontmatter=frontmatter,
        )

        return SynthesisResult(
            artifact_id=f"art_{source_id}",
            title=document.title,
            markdown_content=document.content,
            sources_referenced=[source_id],
        )
