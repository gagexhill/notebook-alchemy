"""Core AlchemyEngine orchestrator."""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyzer import DryRunReport, NotebookAnalyzer, ProgressFn
from .channel import TranscriptManager, YtDlpSubtitles, video_id_from_url
from .client import NotebookLMClient, SourceDocument, normalize_title
from .config import CAPTION_BLOCK_PAUSE, DEFAULT_MAX_PER_VOLUME, AlchemyConfig
from .errors import AlchemyError, ConfigError, SessionError, SyncError, VaultError
from .gemini import GeminiClient
from .synthesizer import (
    CATALOG_FILENAME,
    INDEX_FILENAME,
    DomainCompendium,
    Synthesizer,
    safe_filename,
    source_title,
    unique_slugs,
)
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


@dataclass
class SyncResult:
    notebook_id: str
    created: bool
    uploaded: int
    replaced: int
    pruned: int = 0
    source_limit: int | None = None
    freed_first: bool = False  # old copies were removed before uploading to stay within the limit


@dataclass
class AppendResult:
    added: int
    skipped: int
    updated_files: list[str]


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# `notebooklm` processes run at once during a sync. Each takes seconds to start; notebooklm's own
# help suggests waiting on sources in parallel. Kept small to stay clear of rate limits.
SYNC_WORKERS = 3


def _run_pool(func: Callable[[Any], Any], items: list[Any]) -> list[Any]:
    """Map `func` over `items` on SYNC_WORKERS threads, in order.

    On Ctrl+C, calls not yet started are cancelled; the ones already running finish, because a
    `notebooklm` process cannot be stopped half-way. The interrupt then propagates.
    """
    pool = ThreadPoolExecutor(max_workers=SYNC_WORKERS)
    try:
        return list(pool.map(func, items))
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=True)


class _Counter:
    """Thread-safe progress steps for work that finishes in any order."""

    def __init__(self, progress: ProgressFn | None, label: str, total: int) -> None:
        self._progress, self._label, self._total = progress, label, total
        self._done = 0
        self._lock = threading.Lock()

    def tick(self) -> None:
        with self._lock:
            self._done += 1
            if self._progress:
                self._progress(self._label, self._done, self._total)


def _home_relative(path: Path) -> str:
    """A path with the home folder shown as ~, so pasted output does not carry the user name."""
    for home in (Path.home(), Path.home().resolve()):
        try:
            return str(Path("~") / path.relative_to(home))
        except ValueError:
            continue
    return str(path)


# The working notebook NotebookLM fetches blocked videos' transcripts into, a batch at a time.
WORKSPACE_TITLE = "Notebook Alchemy workspace"
WORKSPACE_BATCH = 20
WORKSPACE_WAIT = 300  # seconds NotebookLM gets to read one video


class AlchemyEngine:
    """Discovery, clustering, synthesis and vault output."""

    def __init__(self, config: AlchemyConfig | None = None) -> None:
        self.config = config or AlchemyConfig.from_env()
        self.client = NotebookLMClient(self.config)
        self.vault = VaultManager(self.config)
        # One client for clustering and synthesis, so a model that fails over stays failed over.
        self.gemini = (
            GeminiClient(
                self.config.gemini_api_key,
                self.config.gemini_model,
                fallback_model=self.config.gemini_fallback_model,
            )
            if self.config.has_gemini_key
            else None
        )
        self.analyzer = NotebookAnalyzer(self.config, client=self.client, gemini=self.gemini)
        self.transcripts = TranscriptManager(
            cache_dir=self.config.cache_dir,
            max_workers=self.config.max_concurrency,
            languages=self.config.caption_languages,
            subtitles=YtDlpSubtitles(
                self.config.caption_languages, cookies_browser=self.config.cookies_browser
            ),
            bulk=self.transcripts_via_notebooklm,
            spacing=self.config.caption_spacing,
            block_pause=CAPTION_BLOCK_PAUSE,
        )
        self.synthesizer = Synthesizer(
            api_key=self.config.gemini_api_key,
            model=self.config.gemini_model,
            fallback_model=self.config.gemini_fallback_model,
            gemini=self.gemini,
            transcripts=self.transcripts,
        )

    def transcripts_via_notebooklm(self, items: list[tuple[str, str]]) -> dict[str, str]:
        """Transcripts NotebookLM fetches on Google's side, for videos YouTube would not give us.

        The videos go into a working notebook in the upload account, a batch at a time; each one's
        text is read back and its source deleted. Returns {video id: text} for those that worked;
        without a NotebookLM sign-in it returns nothing and the videos stay for the next run.
        """
        if not items or not self.client.is_authenticated():
            return {}
        try:
            nb_id, _created = self.client.create_notebook(WORKSPACE_TITLE)
            for leftover in self.client.list_sources(nb_id):  # from an interrupted run
                if leftover.get("id"):
                    self.client.delete_source(nb_id, str(leftover["id"]))
        except AlchemyError as exc:
            logger.warning("NotebookLM could not be used to read transcripts: %s", exc.message)
            return {}
        texts: dict[str, str] = {}
        titles = dict(items)
        for batch in self._chunks(list(titles), WORKSPACE_BATCH):
            try:
                self.client.add_youtube_sources(
                    nb_id, [f"https://www.youtube.com/watch?v={v}" for v in batch]
                )
                queued = [
                    (str(s["id"]), vid)
                    for s in self.client.list_sources(nb_id)
                    if s.get("id") and (vid := self._video_of(s, batch, titles))
                ]
            except AlchemyError as exc:
                logger.warning("NotebookLM did not take %d videos: %s", len(batch), exc.message)
                continue

            def read(pair: tuple[str, str], nb: str = nb_id) -> tuple[str, str]:
                source_id, vid = pair
                text = ""
                try:
                    self.client.wait_for_source(nb, source_id, timeout=WORKSPACE_WAIT)
                    text = self.client.source_text(nb, source_id)
                except AlchemyError as exc:
                    logger.info("NotebookLM could not read %s: %s", vid, exc.message)
                finally:
                    try:
                        self.client.delete_source(nb, source_id)
                    except AlchemyError as exc:  # left for the next run's cleanup, said in the log
                        logger.warning("Could not remove %s from the working notebook: %s", vid, exc.message)
                return vid, text

            for vid, text in _run_pool(read, queued):
                if text.strip():
                    texts[vid] = text
        return texts

    @staticmethod
    def _video_of(source: dict[str, Any], batch: list[str], titles: dict[str, str]) -> str:
        """Which queued video a working-notebook source is: by its URL, else by its title."""
        vid = video_id_from_url(str(source.get("url") or ""))
        if vid in batch:
            return vid
        title = normalize_title(str(source.get("title") or ""))
        return next((v for v in batch if normalize_title(titles.get(v, "")) == title and title), "")

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
            "notebooklm_profile": self.client.profile,
            "session_file": _home_relative(session) if session else "none (run: notebook-alchemy login)",
            "gemini_api_key": "set" if self.config.has_gemini_key else "unset",
            "gemini_model": self.config.gemini_model,
            "vault_path": _home_relative(self.config.vault_path),
            "cache_dir": f"{_home_relative(self.config.cache_dir)} "
            f"({_dir_size(self.config.cache_dir) / 1024:.0f} KB)",
            "max_concurrency": self.config.max_concurrency,
        }

    # -- distillation --

    def run_distillation(
        self,
        notebook_query: str = "",
        *,
        force_offline: bool = False,
        max_videos_per_volume: int = DEFAULT_MAX_PER_VOLUME,
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
            notebook_query,
            use_mock=use_mock,
            limit=limit,
            max_per_section=max_videos_per_volume,
            progress=progress,
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
                progress("Writing documents", done, len(volumes))

        warnings.extend(self._transcript_summary(compendiums))

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

    def _transcript_summary(self, compendiums: list[DomainCompendium]) -> list[str]:
        """One line for the whole run, instead of one per volume, about missing transcripts."""
        lines: list[str] = []
        title_only = [c for c in compendiums if c.title_only]
        blocked = self.transcripts.blocked_total
        if blocked:
            lines.append(
                f"YouTube didn't share captions for {blocked} videos this time. Run the same command "
                "later to fill them in; captions already downloaded are kept."
            )
        if title_only:
            lines.append(
                f"{len(title_only)} of {len(compendiums)} documents list titles and links only, because "
                "their videos had no captions."
            )
        return lines

    @staticmethod
    def _chunks(items: list[Any], size: int) -> list[list[Any]]:
        return [items[i : i + size] for i in range(0, len(items), size)]

    def _write_vault(self, report: DryRunReport, compendiums: list[DomainCompendium], final: Path) -> Path:
        """Build into a staging directory, then swap it in. A failed run leaves the old vault intact."""
        names = [Path(c.relative_path).name for c in compendiums] + [INDEX_FILENAME, CATALOG_FILENAME]
        seen: set[str] = set()
        clashes: set[str] = set()
        for name in names:  # compared without case: Windows and macOS file systems ignore it
            if name.lower() in seen:
                clashes.add(name)
            seen.add(name.lower())
        if clashes:
            raise VaultError(
                f"Two documents would share a file name: {', '.join(sorted(clashes))}.",
                hint="Nothing was written. Report it with these names at "
                "github.com/gagexhill/notebook-alchemy/issues.",
            )
        stage = self.vault.stage_dir(final)
        try:
            for comp in compendiums:
                (stage / Path(comp.relative_path).name).write_text(comp.content, encoding="utf-8")
            (stage / INDEX_FILENAME).write_text(
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
            (stage / CATALOG_FILENAME).write_text(
                self.synthesizer.generate_master_sources_catalog(
                    catalog, notebook_title=report.notebook_title
                ),
                encoding="utf-8",
            )
            self.vault.replace_dir(stage, final)
        except OSError as exc:
            shutil.rmtree(stage, ignore_errors=True)  # best-effort: the real error is raised below
            raise VaultError(f"Could not write the documents folder: {exc}") from exc
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)  # best-effort cleanup before re-raising
            raise
        return final / INDEX_FILENAME

    # -- NotebookLM sync --

    def sync_records_dir(self) -> Path:
        """Where the per-notebook records of past uploads live (`clean --records` empties it)."""
        return self.config.cache_dir.parent / "sync"

    def _sync_state_path(self, notebook_id: str) -> Path:
        """Per-user record of what the last successful sync uploaded to this notebook."""
        return self.sync_records_dir() / f"{safe_filename(notebook_id)}.json"

    def _read_synced_titles(self, notebook_id: str) -> list[str]:
        path = self._sync_state_path(notebook_id)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [str(t) for t in data.get("titles", [])]
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable sync record %s: %s", path, exc)
            return []

    def _write_synced_titles(self, notebook_id: str, titles: list[str]) -> None:
        path = self._sync_state_path(notebook_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"titles": sorted(titles)}), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not record this sync (%s); stale sources will not be pruned next time.", exc
            )

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
                f"No documents found in {source_dir}.",
                hint="Run `notebook-alchemy run <source>` first, or pass --source-dir.",
            )
        for f in files:
            if "sample_data: true" in f.read_text(encoding="utf-8")[:800]:
                raise AlchemyError(
                    f"{source_dir} holds sample data, which is never uploaded.",
                    hint="Run `notebook-alchemy run <youtube-url>` to write documents from a real channel.",
                )

        limits = self.client.plan_limits()
        limit = limits.source_limit if limits else None
        nb_id, created = self.client.create_notebook(notebook_title)
        before = [] if created else [s for s in self.client.list_sources(nb_id) if s.get("id")]

        # Which existing sources this sync replaces or prunes, known before anything is uploaded.
        wanted_titles = {normalize_title(self._source_title_for(f)) for f in files}
        stale_titles = {normalize_title(t) for t in self._read_synced_titles(nb_id)} - wanted_titles
        outgoing = [
            s for s in before if normalize_title(str(s.get("title", ""))) in wanted_titles | stale_titles
        ]
        final_count = len(before) - len(outgoing) + len(files)
        if limit is not None and final_count > limit:
            plan = limits.plan if limits else "unknown plan"
            raise SyncError(
                f"'{notebook_title}' would hold {final_count} sources, but a NotebookLM {plan} notebook "
                f"holds up to {limit}. Nothing was uploaded.",
                failed=[],
                hint="Raise --max-per-vol for fewer, larger documents, sync to a new notebook name with "
                "-n, or remove sources you no longer need from the notebook.",
            )
        freed_first = limit is not None and len(before) + len(files) > limit
        freed: set[str] = set()
        if freed_first:
            # Adding everything before removing the old copies would briefly exceed the limit, so the
            # copies this sync replaces go first. A file that then fails to upload is reported, and
            # the next run uploads it again.
            for old in outgoing:
                try:
                    self.client.delete_source(nb_id, str(old["id"]))
                    freed.add(str(old["id"]))
                except AlchemyError as exc:
                    raise SyncError(
                        f"Could not free a slot in '{notebook_title}' before uploading: {exc.message}",
                        failed=[str(old.get("title", old["id"]))],
                        hint="Remove an old source in NotebookLM, then run the same command again.",
                    ) from exc

        failed: list[str] = []
        steps = _Counter(progress, "Uploading to NotebookLM", len(files) * 2)

        # Each `notebooklm` call is its own process and takes seconds, so uploads and waits run a
        # few at a time. Results are gathered in file order, so the outcome does not depend on
        # which call finishes first; titles are corrected only after every wait has finished.
        def add(md_file: Path) -> tuple[str, str | None, str | None]:
            title = self._source_title_for(md_file)
            content = md_file.read_text(encoding="utf-8")
            try:
                source_id = self.client.add_source_document(
                    nb_id, SourceDocument(md_file.stem, title, content)
                )
                return title, source_id, None
            except AlchemyError as exc:
                return title, None, f"{md_file.name}: {exc.message}"
            except Exception as exc:  # noqa: BLE001 - reported per file, so the rest still reconcile
                return title, None, f"{md_file.name}: {type(exc).__name__}: {exc}"
            finally:
                steps.tick()

        added_results = _run_pool(add, files)
        new_ids: dict[str, str] = {}  # wanted title -> id of the source we just added
        for title, source_id, error in added_results:
            if source_id:
                new_ids[title] = source_id
            if error:
                failed.append(error)

        # NotebookLM can reset a title to the file name once processing ends, so wait for every
        # source, then read the titles back and correct any that changed.
        def wait(item: tuple[str, str]) -> str | None:
            title, source_id = item
            try:
                self.client.wait_for_source(nb_id, source_id)
                return None
            except AlchemyError as exc:
                return f"{title}: processing did not finish: {exc.message}"
            except Exception as exc:  # noqa: BLE001 - reported per source, like the typed errors
                return f"{title}: processing did not finish: {type(exc).__name__}: {exc}"
            finally:
                steps.tick()

        pending = list(new_ids.items())
        waited = _run_pool(wait, pending)
        for (title, _source_id), error in zip(pending, waited, strict=True):
            if error:
                failed.append(error)
                new_ids.pop(title)
        titles_by_id: dict[str, str] = {}
        if new_ids:
            titles_by_id = {str(s["id"]): str(s.get("title", "")) for s in self.client.list_sources(nb_id)}
            for title, source_id in list(new_ids.items()):
                if titles_by_id.get(source_id) != title:
                    try:
                        self.client.rename_source(nb_id, source_id, title)
                        titles_by_id[source_id] = title
                    except AlchemyError as exc:
                        failed.append(f"{title}: could not set the title: {exc.message}")

        # Remove every older source whose title matches one we uploaded, and every source the last
        # sync uploaded that this build no longer contains (domains are re-discovered each run).
        wanted = {normalize_title(t) for t in new_ids}
        stale = {normalize_title(t) for t in self._read_synced_titles(nb_id)} - wanted
        added = set(new_ids.values())
        replaced = pruned = 0
        for old in before:
            key = normalize_title(str(old.get("title", "")))
            if str(old["id"]) in added or (key not in wanted and key not in stale):
                continue
            if str(old["id"]) in freed:
                replaced += key in wanted
                pruned += key not in wanted
                continue
            try:
                self.client.delete_source(nb_id, str(old["id"]))
                if key in wanted:
                    replaced += 1
                else:
                    pruned += 1
            except AlchemyError as exc:
                failed.append(f"{old.get('title', old['id'])}: old copy not removed: {exc.message}")

        if new_ids:  # final read-back: every title exactly once
            counts: dict[str, int] = {}
            for s in self.client.list_sources(nb_id):
                key = normalize_title(str(s.get("title", "")))
                counts[key] = counts.get(key, 0) + 1
            failed.extend(
                f"{title}: appears {counts.get(normalize_title(title), 0)} times in the notebook"
                for title in new_ids
                if counts.get(normalize_title(title), 0) != 1
            )
        if failed:
            raise SyncError(
                f"{len(failed)} problems while uploading {len(files)} files.",
                failed=failed,
                hint="Fix the cause above and run the same command again; it replaces, not duplicates.",
            )
        self._write_synced_titles(nb_id, list(new_ids))
        return SyncResult(nb_id, created, len(new_ids), replaced, pruned, limit, freed_first)

    # -- append --

    @classmethod
    def _source_title_for(cls, path: Path) -> str:
        """The NotebookLM title: the compendium's own domain name, with its part number after the first.

        Taken from the file's frontmatter, because a file name loses characters ("SAM.gov" becomes
        "SAMgov"). The index and catalog, which have no domain, use their file names.
        """
        found = cls._read_domain(path)
        if not found:
            return source_title(path.stem)
        domain, volume = found
        return domain if volume <= 1 else f"{domain} (Part {volume})"

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
        catalog_file = source_dir / CATALOG_FILENAME
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
