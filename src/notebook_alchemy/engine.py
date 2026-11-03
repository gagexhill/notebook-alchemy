"""Core AlchemyEngine orchestrator."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyzer import NotebookAnalyzer
from .client import BaseNotebookClient, SourceDocument
from .config import AlchemyConfig
from .synthesizer import DomainCompendium, Synthesizer
from .vault import VaultManager

logger = logging.getLogger(__name__)


@dataclass
class SynthesisResult:
    """Represents the output of a single knowledge synthesis run."""

    artifact_id: str
    title: str
    markdown_content: str
    sources_referenced: list[str]
    metadata: dict[str, Any] | None = None


@dataclass
class DistillationReport:
    """Report detailing the results of a multi-source distillation run."""

    notebook_title: str
    notebook_id: str
    total_sources_scanned: int
    compendiums: list[DomainCompendium] = field(default_factory=list)
    vault_directory: Path | None = None
    index_path: Path | None = None
    is_live_session: bool = False

    @property
    def total_compendiums(self) -> int:
        return len(self.compendiums)

    @property
    def total_tokens(self) -> int:
        return sum(c.token_count for c in self.compendiums)


class AlchemyEngine:
    """Core knowledge orchestration, clustering, and synthesis engine."""

    def __init__(self, config: AlchemyConfig | None = None) -> None:
        self.config = config or AlchemyConfig.from_env()
        self.client = BaseNotebookClient(self.config)
        self.vault = VaultManager(self.config)
        self.analyzer = NotebookAnalyzer()
        self.synthesizer = Synthesizer(api_key=self.config.gemini_api_key)

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

    def run_distillation(
        self,
        notebook_query: str = "Example Notebook",
        *,
        force_offline: bool = False,
        max_videos_per_volume: int = 50,
        write_to_vault: bool = True,
    ) -> DistillationReport:
        """Run the end-to-end distillation pipeline on a notebook's video sources."""
        # 1. Inspect and cluster sources
        dry_run = self.analyzer.generate_dry_run(notebook_query, use_fallback=True)

        # Map cluster titles back to video source objects
        videos_by_title = {v.title: v for v in dry_run.youtube_videos}

        compendiums: list[DomainCompendium] = []

        for domain, titles in dry_run.domain_topic_clusters.items():
            if not titles:
                continue

            # Batch large clusters into volumes of at most max_videos_per_volume
            for vol_idx in range(0, len(titles), max_videos_per_volume):
                vol_num = (vol_idx // max_videos_per_volume) + 1
                chunk_titles = titles[vol_idx : vol_idx + max_videos_per_volume]
                chunk_videos = [
                    {
                        "id": getattr(videos_by_title.get(t), "source_id", f"src_{i}"),
                        "title": t,
                        "url": getattr(videos_by_title.get(t), "url", "https://youtube.com"),
                    }
                    for i, t in enumerate(chunk_titles)
                ]

                compendium = self.synthesizer.synthesize_cluster(
                    domain=domain,
                    videos=chunk_videos,
                    volume=vol_num,
                    force_offline=force_offline,
                    notebook_title=dry_run.notebook_title,
                    notebook_id=dry_run.notebook_id,
                )
                compendiums.append(compendium)

        vault_dir = self.vault.vault_path / "compendiums"
        index_file: Path | None = None

        # 2. Write compendiums and master index to vault if requested
        if write_to_vault:
            self.vault.ensure_vault()
            vault_dir.mkdir(parents=True, exist_ok=True)

            for comp in compendiums:
                file_path = self.vault.vault_path / comp.relative_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(comp.content, encoding="utf-8")
                logger.info("Saved compendium: %s", file_path)

            # Master index note
            index_content = self.synthesizer.generate_index(
                compendiums=compendiums,
                notebook_title=dry_run.notebook_title,
                total_sources=len(dry_run.youtube_videos),
            )
            index_file = vault_dir / "INDEX.md"
            index_file.write_text(index_content, encoding="utf-8")
            logger.info("Saved master index: %s", index_file)

        return DistillationReport(
            notebook_title=dry_run.notebook_title,
            notebook_id=dry_run.notebook_id,
            total_sources_scanned=dry_run.total_sources,
            compendiums=compendiums,
            vault_directory=vault_dir if write_to_vault else None,
            index_path=index_file,
            is_live_session=not dry_run.is_simulated,
        )
