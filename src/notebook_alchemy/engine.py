"""Core AlchemyEngine orchestrator."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyzer import NotebookAnalyzer, VideoSourceSummary
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

    @property
    def warnings(self) -> list[str]:
        return [
            f"{c.domain} (vol. {c.volume}): {warning}"
            for c in self.compendiums
            for warning in c.warnings
        ]


class AlchemyEngine:
    """Core knowledge orchestration, clustering, and synthesis engine."""

    def __init__(self, config: AlchemyConfig | None = None) -> None:
        self.config = config or AlchemyConfig.from_env()
        self.client = BaseNotebookClient(self.config)
        self.vault = VaultManager(self.config)
        self.analyzer = NotebookAnalyzer(api_key=self.config.gemini_api_key)
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
        notebook_query: str = "Portable Entrepreneur",
        *,
        force_offline: bool = False,
        max_videos_per_volume: int = 50,
        write_to_vault: bool = True,
    ) -> DistillationReport:
        """Run the end-to-end distillation pipeline on a notebook's video sources."""
        # 1. Inspect and cluster sources
        dry_run = self.analyzer.generate_dry_run(
            notebook_query, use_fallback=True, allow_llm=not force_offline
        )

        # Map cluster titles back to video source objects
        videos_by_title: dict[str, list[VideoSourceSummary]] = {}
        for video in dry_run.youtube_videos:
            videos_by_title.setdefault(video.title, []).append(video)

        compendiums: list[DomainCompendium] = []
        used_paths = {"compendiums/index.md", "compendiums/master_sources_catalog.md"}

        for domain, titles in dry_run.domain_topic_clusters.items():
            if not titles:
                continue

            title_offsets: dict[str, int] = {}

            # Batch large clusters into volumes of at most max_videos_per_volume
            for vol_idx in range(0, len(titles), max_videos_per_volume):
                vol_num = (vol_idx // max_videos_per_volume) + 1
                chunk_titles = titles[vol_idx : vol_idx + max_videos_per_volume]
                chunk_videos = []
                for i, title in enumerate(chunk_titles):
                    matches = videos_by_title.get(title, [])
                    offset = title_offsets.get(title, 0)
                    video = matches[offset % len(matches)] if matches else None
                    title_offsets[title] = offset + 1
                    chunk_videos.append(
                        {
                            "id": getattr(video, "source_id", f"src_{i}"),
                            "title": title,
                            "url": getattr(video, "url", ""),
                        }
                    )

                compendium = self.synthesizer.synthesize_cluster(
                    domain=domain,
                    videos=chunk_videos,
                    volume=vol_num,
                    force_offline=force_offline,
                    notebook_title=dry_run.notebook_title,
                    notebook_id=dry_run.notebook_id,
                )
                path = Path(compendium.relative_path)
                suffix = 2
                while compendium.relative_path.casefold() in used_paths:
                    compendium.relative_path = str(
                        path.with_name(f"{path.stem}_{suffix}{path.suffix}")
                    ).replace("\\", "/")
                    suffix += 1
                used_paths.add(compendium.relative_path.casefold())
                compendiums.append(compendium)

        for comp in compendiums:
            related = [
                other for other in compendiums if other is not comp and other.domain != comp.domain
            ]
            if not comp.is_live_synthesized and related:
                comp.content += (
                    "\n## Related Domains\n\n"
                    + "\n".join(
                        f"- [{other.domain}]({Path(other.relative_path).name})" for other in related
                    )
                    + "\n"
                )
                comp.token_count = max(len(comp.content) // 4, 1)

        vault_dir = self.vault.vault_path / "compendiums"
        index_file: Path | None = None

        # 2. Write compendiums and master index to vault if requested
        if write_to_vault:
            self.vault.ensure_vault()
            vault_dir.mkdir(parents=True, exist_ok=True)
            # Purge lingering artifacts from prior runs to prevent cross-channel mixing
            for old_md in vault_dir.glob("*.md"):
                try:
                    old_md.unlink()
                except Exception:  # noqa: BLE001, S110
                    pass

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

            # Master Immutable Source Catalog
            catalog_videos = [
                {
                    "id": v.source_id,
                    "title": v.title,
                    "url": v.url,
                    "topic": "Distilled Source",
                }
                for v in dry_run.youtube_videos
            ]
            catalog_content = self.synthesizer.generate_master_sources_catalog(
                videos=catalog_videos,
                notebook_title=dry_run.notebook_title,
            )
            catalog_file = vault_dir / "MASTER_SOURCES_CATALOG.md"
            catalog_file.write_text(catalog_content, encoding="utf-8")
            logger.info("Saved immutable master catalog: %s", catalog_file)

        return DistillationReport(
            notebook_title=dry_run.notebook_title,
            notebook_id=dry_run.notebook_id,
            total_sources_scanned=dry_run.total_sources,
            compendiums=compendiums,
            vault_directory=vault_dir if write_to_vault else None,
            index_path=index_file,
            is_live_session=not dry_run.is_simulated,
        )

    def sync_to_notebooklm(
        self,
        notebook_title: str = "Portable Entrepreneur",
    ) -> str:
        """Create target NotebookLM notebook if it does not exist, and upload compendiums as sources."""
        # 1. Locate or create destination notebook
        nb_id = self.client.create_notebook(notebook_title)

        # 2. Upload master catalog and all generated compendiums as clean text sources
        vault_dir = self.vault.vault_path / "compendiums"
        if not vault_dir.exists():
            self.run_distillation(notebook_title, force_offline=True, write_to_vault=True)

        uploaded_count = 0
        for md_file in sorted(vault_dir.glob("*.md")):
            doc = SourceDocument(
                source_id=md_file.stem,
                title=md_file.stem.replace("_", " "),
                content=md_file.read_text(encoding="utf-8"),
                source_type="compendium",
            )
            self.client.add_source_document(nb_id, doc)
            uploaded_count += 1

        logger.info(
            "Synced %d compendiums to NotebookLM notebook '%s' (%s)",
            uploaded_count,
            notebook_title,
            nb_id,
        )
        return nb_id

    def append_incremental_sources(
        self,
        new_videos: list[dict[str, Any]],
        notebook_title: str = "Portable Entrepreneur",
    ) -> int:
        """Incrementally append new videos to the Master Catalog and corresponding domain compendiums."""
        vault_dir = self.vault.vault_path / "compendiums"
        vault_dir.mkdir(parents=True, exist_ok=True)

        # 1. Update Master Sources Catalog
        catalog_file = vault_dir / "MASTER_SOURCES_CATALOG.md"
        if catalog_file.exists():
            existing_catalog = catalog_file.read_text(encoding="utf-8")
            append_lines = []
            for v in new_videos:
                vid_id = v.get("id") or "new_yt"
                title = (v.get("title") or "New Video").replace("|", "-")
                url = v.get("url") or "https://youtube.com"
                topic = v.get("topic") or "General"
                append_lines.append(f"| + | `{vid_id}` | {title} | {topic} | [Link]({url}) |")
            catalog_file.write_text(
                existing_catalog + "\n" + "\n".join(append_lines) + "\n", encoding="utf-8"
            )
        else:
            cat_content = self.synthesizer.generate_master_sources_catalog(
                new_videos, notebook_title
            )
            catalog_file.write_text(cat_content, encoding="utf-8")

        # 2. Cluster new videos and auto-append to domain compendiums
        titles = [str(v.get("title", "")) for v in new_videos]
        clusters = self.analyzer._cluster_topics(titles)
        updated_compendiums = 0

        for domain, d_titles in clusters.items():
            if not d_titles:
                continue
            safe_name = self.synthesizer._safe_filename(domain)
            target_comp = vault_dir / f"{safe_name}.md"
            matching_videos = [v for v in new_videos if v.get("title") in d_titles]

            if target_comp.exists():
                old_text = target_comp.read_text(encoding="utf-8")
                new_text = self.synthesizer.append_to_domain_compendium(
                    old_text, matching_videos, domain=domain
                )
                target_comp.write_text(new_text, encoding="utf-8")
                updated_compendiums += 1
            else:
                comp = self.synthesizer.synthesize_cluster(
                    domain, matching_videos, volume=1, force_offline=True
                )
                target_comp.write_text(comp.content, encoding="utf-8")
                updated_compendiums += 1

        return updated_compendiums
