"""Core AlchemyEngine orchestrator."""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyzer import DryRunReport, NotebookAnalyzer, ProgressFn
from .channel import TranscriptManager
from .client import NotebookLMClient, SourceDocument
from .config import AlchemyConfig
from .errors import AlchemyError, ConfigError, SessionError, SyncError, VaultError
from .synthesizer import DomainCompendium, Synthesizer, safe_filename, unique_slugs
from .vault import VaultManager

logger = logging.getLogger(__name__)

MAX_RELATED_LINKS = 12


@dataclass
class DistillationReport:
    """What a distillation run produced."""

    notebook_title: str
    notebook_id: str
    total_sources_scanned: int
    compendiums: list[DomainCompendium] = field(default_factory=list)
    vault_directory: Path | None = None
    index_path: Path | None = None
    is_mock: bool = False
    cluster_method: str = "keyword"
    warnings: list[str] = field(default_factory=list)

    @property
    def total_compendiums(self) -> int:
        return len(self.compendiums)

    @property
    def total_tokens(self) -> int:
        return sum(c.token_count for c in self.compendiums)

    @property
    def live_compendiums(self) -> int:
        return sum(1 for c in self.compendiums if c.is_live_synthesized)


@dataclass
class SyncResult:
    notebook_id: str
    created: bool
    uploaded: int
    replaced: int


@dataclass
class AppendResult:
    added: int
    skipped: int
    updated_files: list[str]


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class AlchemyEngine:
    """Discovery, clustering, synthesis and vault output."""

    def __init__(self, config: AlchemyConfig | None = None) -> None:
        self.config = config or AlchemyConfig.from_env()
        self.client = NotebookLMClient(self.config)
        self.vault = VaultManager(self.config)
        self.analyzer = NotebookAnalyzer(self.config, client=self.client)
        self.transcripts = TranscriptManager(
            cache_dir=self.config.cache_dir, max_workers=self.config.max_concurrency
        )
        self.synthesizer = Synthesizer(
            api_key=self.config.gemini_api_key,
            model=self.config.gemini_model,
            fallback_model=self.config.gemini_fallback_model,
            transcripts=self.transcripts,
        )

    def analyzer_for(self, offline: bool) -> NotebookAnalyzer:
        """The analyzer, set to use Gemini only when `offline` is false."""
        self.analyzer.force_offline = offline
        return self.analyzer

    def status(self) -> dict[str, Any]:
        """Facts about this installation. Nothing here contacts the network."""
        session = self.client.session_file()
        return {
            "environment": self.config.env,
            "notebooklm_cli": self.client.cli_version() or "not found (pip install notebooklm-py)",
            "session_file": str(session) if session else "none (run: notebook-alchemy login)",
            "gemini_api_key": "set" if self.config.has_gemini_key else "unset",
            "gemini_model": self.config.gemini_model,
            "vault_path": str(self.config.vault_path),
            "cache_dir": f"{self.config.cache_dir} ({_dir_size(self.config.cache_dir) / 1024:.0f} KB)",
            "max_concurrency": self.config.max_concurrency,
        }

    # -- distillation --

    def run_distillation(
        self,
        notebook_query: str = "",
        *,
        force_offline: bool = False,
        max_videos_per_volume: int = 50,
        write_to_vault: bool = True,
        use_mock: bool = False,
        limit: int | None = None,
        dry_run: DryRunReport | None = None,
        progress: ProgressFn | None = None,
    ) -> DistillationReport:
        """Cluster a source and build one compendium per domain volume."""
        if max_videos_per_volume < 1:
            raise ConfigError(
                f"--max-per-vol must be 1 or more, got {max_videos_per_volume}.",
                hint="The default is 50.",
            )
        self.analyzer.force_offline = force_offline
        report = dry_run or self.analyzer.generate_dry_run(
            notebook_query, use_mock=use_mock, limit=limit, progress=progress
        )
        if not report.youtube_videos:
            raise AlchemyError(
                f"'{report.notebook_title}' has no video sources to distill.",
                hint="Check the URL or notebook, or pass a different source.",
            )

        domains = list(report.domain_videos)
        slugs = unique_slugs(domains)
        all_links = [(slugs[d], d) for d in domains]
        volumes = [
            (domain, vol, chunk)
            for domain in domains
            for vol, chunk in enumerate(self._chunks(report.domain_videos[domain], max_videos_per_volume), 1)
        ]

        warnings = list(report.notes)
        compendiums: list[DomainCompendium] = []
        for done, (domain, vol, chunk) in enumerate(volumes, 1):
            related = [(s, n) for s, n in all_links if n != domain][:MAX_RELATED_LINKS]
            compendium = self.synthesizer.synthesize_cluster(
                domain=domain,
                videos=[{"id": v.source_id, "title": v.title, "url": v.url} for v in chunk],
                volume=vol,
                force_offline=force_offline,
                notebook_title=report.notebook_title,
                notebook_id=report.notebook_id,
                slug=slugs[domain],
                related_domains=related,
                progress=progress,
            )
            warnings.extend(compendium.warnings)
            compendiums.append(compendium)
            if progress:
                progress("Building compendiums", done, len(volumes))

        vault_dir = self.vault.compendium_dir(report.notebook_title)
        index_file: Path | None = None
        if write_to_vault:
            index_file = self._write_vault(report, compendiums, vault_dir)

        return DistillationReport(
            notebook_title=report.notebook_title,
            notebook_id=report.notebook_id,
            total_sources_scanned=report.total_sources,
            compendiums=compendiums,
            vault_directory=vault_dir if write_to_vault else None,
            index_path=index_file,
            is_mock=report.is_mock,
            cluster_method=report.cluster_method,
            warnings=warnings,
        )

    @staticmethod
    def _chunks(items: list[Any], size: int) -> list[list[Any]]:
        return [items[i : i + size] for i in range(0, len(items), size)]

    def _write_vault(self, report: DryRunReport, compendiums: list[DomainCompendium], final: Path) -> Path:
        """Build into a staging directory, then swap it in. A failed run leaves the old vault intact."""
        stage = self.vault.stage_dir(final)
        try:
            for comp in compendiums:
                (stage / Path(comp.relative_path).name).write_text(comp.content, encoding="utf-8")
            (stage / "INDEX.md").write_text(
                self.synthesizer.generate_index(
                    compendiums,
                    notebook_title=report.notebook_title,
                    total_sources=len(report.youtube_videos),
                ),
                encoding="utf-8",
            )
            domain_of = {v.source_id: domain for domain, vs in report.domain_videos.items() for v in vs}
            catalog = [
                {
                    "id": v.source_id,
                    "title": v.title,
                    "url": v.url,
                    "topic": domain_of.get(v.source_id, ""),
                }
                for v in report.youtube_videos
            ]
            (stage / "MASTER_SOURCES_CATALOG.md").write_text(
                self.synthesizer.generate_master_sources_catalog(
                    catalog, notebook_title=report.notebook_title
                ),
                encoding="utf-8",
            )
            self.vault.replace_dir(stage, final)
        except OSError as exc:
            shutil.rmtree(stage, ignore_errors=True)  # best-effort: the real error is raised below
            raise VaultError(f"Could not write the vault: {exc}") from exc
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)  # best-effort cleanup before re-raising
            raise
        return final / "INDEX.md"

    # -- NotebookLM sync --

    def sync_to_notebooklm(
        self,
        notebook_title: str,
        source_dir: Path,
        *,
        progress: ProgressFn | None = None,
    ) -> SyncResult:
        """Upload every compendium in `source_dir`, replacing same-titled sources."""
        status = self.client.check_session()
        if not status.ok:
            raise SessionError(status.detail, hint="Run `notebook-alchemy login`.")
        files = sorted(source_dir.glob("*.md")) if source_dir.is_dir() else []
        if not files:
            raise VaultError(
                f"No compendiums found in {source_dir}.",
                hint="Run `notebook-alchemy run <source>` first, or pass --source-dir.",
            )
        for f in files:
            if 'notebook_id: "mock_' in f.read_text(encoding="utf-8")[:800]:
                raise AlchemyError(
                    f"{source_dir} holds mock sample data; it will not be uploaded.",
                    hint="Run `notebook-alchemy run <real source>` to build real compendiums.",
                )

        nb_id, created = self.client.create_notebook(notebook_title)
        existing: dict[str, str] = {}
        if not created:
            existing = {
                str(s.get("title", "")): str(s["id"]) for s in self.client.list_sources(nb_id) if s.get("id")
            }

        failed: list[str] = []
        uploaded = replaced = 0
        for done, md_file in enumerate(files, 1):
            title = md_file.stem.replace("_", " ")
            doc = SourceDocument(
                source_id=md_file.stem,
                title=title,
                content=md_file.read_text(encoding="utf-8"),
                source_type="compendium",
            )
            try:
                self.client.add_source_document(nb_id, doc)
                uploaded += 1
                if title in existing:
                    self.client.delete_source(nb_id, existing[title])
                    replaced += 1
            except AlchemyError as exc:
                failed.append(f"{md_file.name}: {exc.message}")
            if progress:
                progress("Uploading to NotebookLM", done, len(files))
        if failed:
            raise SyncError(
                f"{len(failed)} of {len(files)} files did not upload.",
                failed=failed,
                hint="Fix the cause above and run the same command again; it replaces, not duplicates.",
            )
        return SyncResult(nb_id, created, uploaded, replaced)

    # -- append --

    @staticmethod
    def _read_domain(path: Path) -> tuple[str, int] | None:
        head = path.read_text(encoding="utf-8")[:1500]
        dm = re.search(r"^domain:\s*(\".*\")\s*$", head, re.MULTILINE)
        vm = re.search(r"^volume:\s*(\d+)", head, re.MULTILINE)
        if not dm:
            return None
        try:
            return json.loads(dm.group(1)), int(vm.group(1)) if vm else 1
        except ValueError:
            return None

    def append_incremental_sources(
        self,
        new_videos: list[dict[str, Any]],
        source_dir: Path,
        *,
        domain: str | None = None,
        notebook_title: str = "Untitled",
    ) -> AppendResult:
        """Add videos to the catalog and to the matching domain compendium. Duplicates are skipped."""
        source_dir.mkdir(parents=True, exist_ok=True)
        catalog_file = source_dir / "MASTER_SOURCES_CATALOG.md"
        catalog_text = catalog_file.read_text(encoding="utf-8") if catalog_file.exists() else ""

        fresh: list[dict[str, Any]] = []
        for v in new_videos:
            vid, url = str(v.get("id") or ""), str(v.get("url") or "")
            if (vid and f"`{vid}`" in catalog_text) or (url and url in catalog_text):
                continue
            fresh.append(v)
        skipped = len(new_videos) - len(fresh)
        if not fresh:
            return AppendResult(0, skipped, [])

        titles = [str(v.get("title") or "") for v in fresh]
        if domain:
            assignment = {domain: list(range(len(fresh)))}
        else:
            self.analyzer.force_offline = not self.config.has_gemini_key
            clusters = self.analyzer.cluster_titles(titles)
            assignment = {}
            used: set[int] = set()
            for name, cluster_titles in clusters.items():
                idxs = []
                for t in cluster_titles:
                    i = next(k for k, tt in enumerate(titles) if tt == t and k not in used)
                    used.add(i)
                    idxs.append(i)
                assignment[name] = idxs

        rows = []
        for name, idxs in assignment.items():
            for i in idxs:
                v = fresh[i]
                title = str(v.get("title") or "New Video").replace("|", "-")
                rows.append(
                    f"| + | `{v.get('id') or 'new'}` | {title} | {name.replace('|', '-')} | "
                    f"[Link]({v.get('url') or ''}) |"
                )
        if catalog_text:
            catalog_text = re.sub(
                r"^total_sources:\s*(\d+)",
                lambda m: f"total_sources: {int(m.group(1)) + len(fresh)}",
                catalog_text,
                count=1,
                flags=re.MULTILINE,
            )
            catalog_file.write_text(
                catalog_text.rstrip("\n") + "\n" + "\n".join(rows) + "\n", encoding="utf-8"
            )
        else:
            seed = [
                {**v, "topic": next(n for n, ix in assignment.items() if i in ix)}
                for i, v in enumerate(fresh)
            ]
            catalog_file.write_text(
                self.synthesizer.generate_master_sources_catalog(seed, notebook_title),
                encoding="utf-8",
            )

        existing: dict[str, list[tuple[int, Path]]] = {}
        for f in source_dir.glob("*.md"):
            info = self._read_domain(f)
            if info:
                existing.setdefault(info[0].lower(), []).append((info[1], f))
        taken = {f.stem.lower() for f in source_dir.glob("*.md")}

        updated: list[str] = []
        for name, idxs in assignment.items():
            matching = [fresh[i] for i in idxs]
            versions = existing.get(name.lower())
            if versions:
                target = max(versions)[1]
                target.write_text(
                    self.synthesizer.append_to_domain_compendium(
                        target.read_text(encoding="utf-8"), matching, domain=name
                    ),
                    encoding="utf-8",
                )
            else:
                slug = safe_filename(name)
                if slug.lower() in taken:
                    slug = f"{slug}_new"
                taken.add(slug.lower())
                comp = self.synthesizer.synthesize_cluster(
                    name,
                    matching,
                    volume=1,
                    force_offline=True,
                    notebook_title=notebook_title,
                    slug=slug,
                )
                target = source_dir / f"{slug}.md"
                target.write_text(comp.content, encoding="utf-8")
            updated.append(target.name)
        return AppendResult(len(fresh), skipped, updated)
