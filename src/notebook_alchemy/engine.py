"""The pipeline: a channel link in, topic books in a NotebookLM notebook out.

1. Find every video (with a date).
2. Get every video's words: captions, yt-dlp subtitles, then NotebookLM on Google's side.
3. Clean them locally and keep each repeated passage once.
4. Label each video once with Gemini (keep or drop, headline, kind); cached.
5. Sort passages into topics and sections, consolidate each section, apply the date rules.
6. Write one book per topic plus an overview, and upload them; nothing is kept on disk.

Every paid step is cached under ~/.notebook-alchemy, so a rerun pays only for what changed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .channel import ChannelScanner, ChannelVideo, TranscriptManager, YtDlpSubtitles, video_id_from_url
from .cleanup import Chunk, chunk_video, is_punctuated, mark_duplicates
from .client import NotebookLMClient, SourceDocument, normalize_title
from .config import CAPTION_BLOCK_PAUSE, AlchemyConfig
from .errors import AlchemyError, ChannelScanError, GeminiError, SessionError, SyncError
from .gemini import GeminiClient, price_for
from .knowledge import KnowledgeStore, Labeler, VideoText, label_key
from .library import Library, LibraryBuilder, Passage, RequestCache, build_passages
from .mock_data import MOCK_NOTEBOOK_TITLE, get_mock_sources, sample_transcript
from .offline import library_without_ai
from .writer import Book, render_library

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int], None]

# The working notebook NotebookLM fetches blocked videos' transcripts into, a batch at a time.
WORKSPACE_TITLE = "Notebook Alchemy workspace"
WORKSPACE_KEY = "__workspace__"  # its id in notebooks.json, so a notebook of yours with that name is not used
WORKSPACE_BATCH = 20
WORKSPACE_WAIT = 300  # seconds NotebookLM gets to read one video

# Estimates for dry-run: words spoken per second, tokens per word, and the share of a transcript
# the label answer carries (labels only, or the words again when punctuation is added).
WORDS_PER_SECOND = 2.5
TOKENS_PER_WORD = 1.35
LABEL_OUTPUT_SHARE = 0.05
PUNCTUATED_OUTPUT_SHARE = 0.75
SORTING_SHARE = 0.15  # sorting and consolidation, as a share of the labelling cost

# `notebooklm` processes run at once during an upload. Kept small to stay clear of rate limits.
SYNC_WORKERS = 3


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
class Estimate:
    """What a run would read and roughly cost, before anything is spent."""

    videos: int
    words_known: int  # words already read (cache)
    videos_known: int
    words_estimated: int  # known words plus an estimate from duration for the rest
    labelled: int  # videos whose labels are cached already
    cost_usd: float
    model: str


@dataclass
class BuildReport:
    channel: str
    videos: list[ChannelVideo]
    read: set[str]
    labelled: int
    waiting: list[str]
    library: Library
    books: list[Book]
    notices: list[str] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)
    is_mock: bool = False
    used_ai: bool = True


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


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _home_relative(path: Path) -> str:
    """A path with the home folder shown as ~, so pasted output does not carry the user name."""
    for home in (Path.home(), Path.home().resolve()):
        try:
            return str(Path("~") / path.relative_to(home))
        except ValueError:
            continue
    return str(path)


def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)[:80] or "x"


class AlchemyEngine:
    """Runs the pipeline; holds the clients and the caches."""

    def __init__(self, config: AlchemyConfig | None = None) -> None:
        self.config = config or AlchemyConfig.from_env()
        self.client = NotebookLMClient(self.config)
        self.gemini = (
            GeminiClient(
                self.config.gemini_api_key,
                self.config.gemini_model,
                fallback_model=self.config.gemini_fallback_model,
            )
            if self.config.has_gemini_key
            else None
        )
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

    # -- where things are kept --

    @property
    def home(self) -> Path:
        return self.config.cache_dir.parent

    def knowledge_dir(self) -> Path:
        return self.home / "knowledge"

    def library_cache_path(self, channel: str) -> Path:
        return self.home / "library" / f"{_safe_name(channel)}.json"

    def sync_records_dir(self) -> Path:
        """Per notebook: the titles the last upload added (`clean --records` empties it)."""
        return self.home / "sync"

    def notebooks_path(self) -> Path:
        """Channel → notebook id, so a rerun updates the same notebook."""
        return self.home / "notebooks.json"

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
            "cache_dir": f"{_home_relative(self.home)} ({_dir_size(self.home) / 1024:.0f} KB)",
            "max_concurrency": self.config.max_concurrency,
        }

    # -- 1. videos --

    def scan(
        self, source: str, *, limit: int | None, tabs: tuple[str, ...], mock: bool
    ) -> tuple[str, list[ChannelVideo]]:
        if mock:
            sources = get_mock_sources()[:limit] if limit else get_mock_sources()
            return MOCK_NOTEBOOK_TITLE, [
                ChannelVideo(s["id"], s["title"], s["url"], 600, "2026-01-01") for s in sources
            ]
        if not ChannelScanner().is_youtube_url(source):
            raise ChannelScanError(
                f"'{source}' is not a YouTube link.",
                hint="Give a channel (@handle or its URL), a playlist or a video URL.",
            )
        scan = ChannelScanner().scan(source, limit=limit, tabs=tabs)
        return scan.channel_name, scan.videos

    def estimate(self, videos: list[ChannelVideo], *, mock: bool = False) -> Estimate:
        """Words already read and an estimated cost for the rest, from durations. Spends nothing."""
        model = self.config.gemini_model
        price_in, price_out, _note = price_for(model)
        known_words = known_videos = labelled = 0
        words_total = 0
        cost = 0.0
        store = KnowledgeStore(self.knowledge_dir())
        for v in videos:
            cached = None if mock else self.transcripts._read_cache(v.id)
            if cached and cached.text:
                words = len(cached.text.split())
                known_words += words
                known_videos += 1
                punctuated = is_punctuated(cached.text)
                if store.get(v.id, label_key(v.id, cached.text, model)) is not None:
                    labelled += 1
                    words_total += words
                    continue
            else:
                words = int((v.duration_seconds or 600) * WORDS_PER_SECOND)
                punctuated = False
            words_total += words
            tokens = words * TOKENS_PER_WORD
            share = LABEL_OUTPUT_SHARE + (0 if punctuated else PUNCTUATED_OUTPUT_SHARE)
            cost += (tokens * price_in + tokens * share * price_out) / 1e6
        cost *= 1 + SORTING_SHARE
        return Estimate(len(videos), known_words, known_videos, words_total, labelled, cost, model)

    # -- 2.-6. build --

    def build(
        self,
        source: str,
        *,
        limit: int | None = None,
        tabs: tuple[str, ...] = ("videos", "streams"),
        mock: bool = False,
        offline: bool = False,
        resort: bool = False,
        use_batch: bool = True,
        progress: ProgressFn | None = None,
    ) -> BuildReport:
        channel, videos = self.scan(source, limit=limit, tabs=tabs, mock=mock)
        today = dt.datetime.now(dt.UTC).date().isoformat()
        notices: list[str] = []

        # 2. words
        if mock:
            texts = {v.id: sample_transcript({"title": v.title}) for v in videos}
            exact: set[str] = set()
            by_source = {"sample": len(videos)}
        else:
            results = self.transcripts.fetch_many(
                [(v.id, v.title) for v in videos],
                progress=(lambda d, t: progress("Reading what each video says", d, t)) if progress else None,
            )
            texts = {vid: r.text for vid, r in results.items() if r.text}
            exact = {vid for vid, r in results.items() if r.published}
            for v in videos:  # an exact date from yt-dlp replaces the listing's approximate one
                if results.get(v.id) and results[v.id].published:
                    v.published = results[v.id].published
            by_source = dict(self.transcripts.by_source)
            notices += self.transcripts.notices
            if self.transcripts.blocked_total:
                notices.append(
                    f"YouTube wouldn't share words for {self.transcripts.blocked_total} videos this time. "
                    "Run the same command later to fill them in; words already read are kept."
                )
        read = set(texts)

        # 3. clean and dedupe
        chunks: dict[str, list[Chunk]] = {vid: chunk_video(vid, text) for vid, text in texts.items()}
        dated = {v.id: v.published for v in videos}
        mark_duplicates([(dated.get(vid, ""), cs) for vid, cs in chunks.items()])

        use_ai = self.gemini is not None and not offline and not mock
        waiting: list[str] = []
        labelled = 0
        passages: list[Passage] = []
        titles = {v.id: v.title for v in videos}
        if use_ai:
            assert self.gemini is not None  # noqa: S101 - use_ai implies it
            # 4. labels
            labeler = Labeler(
                self.gemini,
                KnowledgeStore(self.knowledge_dir()),
                today,
                state_path=self.home / "batch.json",
                use_batch=use_batch,
            )
            to_label = [
                VideoText(vid, titles.get(vid, ""), dated.get(vid, ""), texts[vid], [c for c in cs if c.kept])
                for vid, cs in chunks.items()
            ]
            run = labeler.run(
                to_label, progress=(lambda d, t: progress("Labelling", d, t)) if progress else None
            )
            notices += run.notices
            waiting, labelled = run.waiting, run.labelled_now
            for vid, cs in chunks.items():
                if vid in run.labels:
                    passages += build_passages(
                        vid, titles.get(vid, ""), dated.get(vid, ""), cs, run.labels[vid]
                    )
            # 5. library
            builder = LibraryBuilder(
                self.gemini, RequestCache(self.library_cache_path(channel)), today, channel
            )
            try:
                library = builder.build(passages, list(titles.values()), resort=resort, progress=progress)
            except GeminiError as exc:
                if not exc.quota:
                    raise
                raise AlchemyError(
                    f"Gemini's daily request quota is used up. {len(run.labels)} of {len(chunks)} videos are "
                    "labelled and saved; nothing was uploaded.",
                    hint="Run the same command again after the quota resets; it carries on from here. Enable "
                    "billing on the key's Google Cloud project to finish in one run.",
                ) from exc
            notices += builder.notices
        else:
            for vid, cs in chunks.items():
                # Without labels, a video's kept words are one passage, headed by its title.
                for p in build_passages(vid, titles.get(vid, ""), dated.get(vid, ""), cs, None):
                    p.headline = p.title
                    passages.append(p)
            library = library_without_ai(channel, passages)

        # 6. books
        books = render_library(library, channel, titles, read - set(waiting), exact, dated)
        return BuildReport(
            channel=channel,
            videos=videos,
            read=read,
            labelled=labelled,
            waiting=waiting,
            library=library,
            books=books,
            notices=notices,
            by_source=by_source,
            is_mock=mock,
            used_ai=use_ai,
        )

    @staticmethod
    def save_books(books: list[Book], folder: Path) -> list[Path]:
        """Write the books as Markdown files (the opt-in --save)."""
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for book in books:
            path = folder / f"{_safe_name(book.title)}.md"
            path.write_text(book.content, encoding="utf-8")
            paths.append(path)
        return paths

    # -- transcripts through NotebookLM --

    def transcripts_via_notebooklm(self, items: list[tuple[str, str]]) -> dict[str, str]:
        """Transcripts NotebookLM fetches on Google's side, for videos YouTube would not give us.

        The videos go into a working notebook in the upload account, a batch at a time; each one's
        text is read back and its source deleted. Returns {video id: text} for those that worked;
        without a NotebookLM sign-in it returns nothing and the videos stay for the next run.
        """
        if not items or not self.client.is_authenticated():
            return {}
        titles = dict(items)
        wanted = list(titles)
        try:
            recorded = self.notebook_for(WORKSPACE_KEY)
            if recorded and any(str(n.get("id")) == recorded for n in self.client.list_notebooks()):
                nb_id = recorded
            else:
                nb_id, _created = self.client.create_notebook(WORKSPACE_TITLE)
                self.remember_notebook(WORKSPACE_KEY, nb_id)
            existing = self.client.list_sources(nb_id)
        except AlchemyError as exc:
            logger.warning("NotebookLM could not be used to read transcripts: %s", exc.message)
            return {}
        # YouTube sources left by an interrupted run: read the ones still wanted, remove the rest.
        # Anything else in the notebook is not this tool's and is left alone.
        leftovers: list[tuple[str, str]] = []
        for source in existing:
            if not source.get("id") or not self._is_youtube_source(source):
                continue
            vid = self._video_of(source, wanted, titles)
            if vid:
                leftovers.append((str(source["id"]), vid))
            else:
                self._remove_workspace_source(nb_id, str(source["id"]), "an old video")
        texts = self._read_workspace(nb_id, leftovers)
        started = {vid for _sid, vid in leftovers}
        pending = [v for v in wanted if v not in started]
        for batch in [pending[i : i + WORKSPACE_BATCH] for i in range(0, len(pending), WORKSPACE_BATCH)]:
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
            texts.update(self._read_workspace(nb_id, queued))
        return texts

    def _read_workspace(self, nb_id: str, pairs: list[tuple[str, str]]) -> dict[str, str]:
        """Wait for each (source id, video id), read its text, and remove the source."""

        def read(pair: tuple[str, str]) -> tuple[str, str]:
            source_id, vid = pair
            text = ""
            try:
                self.client.wait_for_source(nb_id, source_id, timeout=WORKSPACE_WAIT)
                text = self.client.source_text(nb_id, source_id)
            except AlchemyError as exc:  # no transcript NotebookLM can read: the video waits
                logger.info("NotebookLM could not read %s: %s", vid, exc.message)
            finally:
                self._remove_workspace_source(nb_id, source_id, vid)
            return vid, text

        return {vid: text for vid, text in _run_pool(read, pairs) if text.strip()}

    def _remove_workspace_source(self, nb_id: str, source_id: str, what: str) -> None:
        try:
            self.client.delete_source(nb_id, source_id)
        except AlchemyError as exc:  # the next run removes it; said in the log
            logger.warning("Could not remove %s from the working notebook: %s", what, exc.message)

    @staticmethod
    def _video_of(source: dict[str, Any], batch: list[str], titles: dict[str, str]) -> str:
        """Which queued video a working-notebook source is: by its URL, else by its title.

        While NotebookLM is still fetching a video, the source has no URL and its title is the link.
        """
        for field_name in ("url", "title"):
            vid = video_id_from_url(str(source.get(field_name) or ""))
            if vid in batch:
                return vid
        title = normalize_title(str(source.get("title") or ""))
        matches = [v for v in batch if title and normalize_title(titles.get(v, "")) == title]
        # By title only when one queued video has it: two "Live Q&A" videos must not swap words.
        return matches[0] if len(matches) == 1 else ""

    @staticmethod
    def _is_youtube_source(source: dict[str, Any]) -> bool:
        if str(source.get("type") or "").lower() == "youtube":
            return True
        return any(
            not video_id_from_url(str(source.get(f) or "")).startswith("url_")
            for f in ("url", "title")
            if str(source.get(f) or "").startswith("http")
        )

    # -- records --

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable record %s: %s", path, exc)
            return {}

    def _write_json(self, path: Path, data: dict[str, Any], what: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not record %s (%s).", what, exc)

    def _sync_state_path(self, notebook_id: str) -> Path:
        return self.sync_records_dir() / f"{_safe_name(notebook_id)}.json"

    def _read_record(self, notebook_id: str) -> dict[str, Any]:
        """What this tool uploaded to a notebook: {"titles": [...], "ids": {title: source id}}."""
        data = self._read_json(self._sync_state_path(notebook_id))
        ids = data.get("ids") if isinstance(data.get("ids"), dict) else {}
        return {
            "titles": [str(t) for t in data.get("titles", [])],
            "ids": {str(k): str(v) for k, v in ids.items()},
        }

    def _write_record(self, notebook_id: str, ids: dict[str, str]) -> None:
        self._write_json(
            self._sync_state_path(notebook_id), {"titles": sorted(ids), "ids": ids}, "this upload"
        )

    def notebook_for(self, channel_key: str) -> str | None:
        return self._read_json(self.notebooks_path()).get(channel_key)

    def remember_notebook(self, channel_key: str, notebook_id: str) -> None:
        data = self._read_json(self.notebooks_path())
        data[channel_key] = notebook_id
        self._write_json(self.notebooks_path(), data, "the notebook for this channel")

    # -- upload --

    def publish(
        self,
        notebook_title: str,
        books: list[Book],
        *,
        channel_key: str | None = None,
        progress: ProgressFn | None = None,
    ) -> SyncResult:
        """Upload the books to the channel's notebook, replacing earlier copies; nothing is kept on disk.

        The notebook is the one recorded for `channel_key` when it still exists (so a new AI title
        does not start a second notebook), else one titled `notebook_title`, created if needed.
        """
        status = self.client.check_session()
        if not status.ok:
            raise SessionError(status.detail, hint="Run `notebook-alchemy login`.")
        if not books:
            raise AlchemyError(
                "There is nothing to upload.", hint="Check that the videos have words to read."
            )
        limits = self.client.plan_limits()
        limit = limits.source_limit if limits else None
        nb_id, created = "", False
        recorded = self.notebook_for(channel_key) if channel_key else None
        if recorded and any(str(n.get("id")) == recorded for n in self.client.list_notebooks()):
            nb_id = recorded
        else:
            nb_id, created = self.client.create_notebook(notebook_title)
        if channel_key:
            self.remember_notebook(channel_key, nb_id)
        before = [] if created else [s for s in self.client.list_sources(nb_id) if s.get("id")]

        # Only sources this tool uploaded are ever replaced or removed: by the ids it recorded, or, for
        # a record from version 0.2 (titles only), by those titles. A source of yours with the same
        # title as a book is left alone.
        record = self._read_record(nb_id)
        mine_ids = set(record["ids"].values())
        legacy = {normalize_title(t) for t in record["titles"]} if not record["ids"] else set()
        ours = [
            s
            for s in before
            if str(s["id"]) in mine_ids or normalize_title(str(s.get("title", ""))) in legacy
        ]
        book_titles = {normalize_title(b.title) for b in books}
        final_count = len(before) - len(ours) + len(books)
        if limit is not None and final_count > limit:
            plan = limits.plan if limits else "unknown plan"
            raise SyncError(
                f"The notebook would hold {final_count} sources, but a NotebookLM {plan} notebook holds "
                f"up to {limit}. Nothing was uploaded.",
                failed=[],
                hint="Remove sources you no longer need from the notebook, or upload to a new notebook "
                "with -N.",
            )
        freed_first = limit is not None and len(before) + len(books) > limit
        freed: set[str] = set()
        if freed_first:
            # Adding everything before removing the old copies would briefly exceed the limit, so
            # the copies this upload replaces go first.
            for old in ours:
                try:
                    self.client.delete_source(nb_id, str(old["id"]))
                    freed.add(str(old["id"]))
                except AlchemyError as exc:
                    raise SyncError(
                        f"Could not free a slot before uploading: {exc.message}",
                        failed=[str(old.get("title", old["id"]))],
                        hint="Remove an old source in NotebookLM, then run the same command again.",
                    ) from exc

        failed: list[str] = []
        steps = _Counter(progress, "Uploading to NotebookLM", len(books) * 2)

        def add(book: Book) -> tuple[str, str | None, str | None]:
            try:
                source_id = self.client.add_source_document(
                    nb_id, SourceDocument(_safe_name(book.title), book.title, book.content)
                )
                return book.title, source_id, None
            except AlchemyError as exc:
                return book.title, None, f"{book.title}: {exc.message}"
            except Exception as exc:  # noqa: BLE001 - reported per book, so the rest still reconcile
                return book.title, None, f"{book.title}: {type(exc).__name__}: {exc}"
            finally:
                steps.tick()

        new_ids: dict[str, str] = {}
        for title, source_id, error in _run_pool(add, books):
            if source_id:
                new_ids[title] = source_id
            if error:
                failed.append(error)
        sizes = {b.title: b.words for b in books}

        def wait(item: tuple[str, str]) -> str | None:
            title, source_id = item
            # A large book takes NotebookLM longer to process: a minute per 50,000 words, at least 3.
            timeout = max(180, int(sizes.get(title, 0) / 50_000 * 60) + 180)
            try:
                self.client.wait_for_source(nb_id, source_id, timeout=timeout)
                return None
            except AlchemyError as exc:
                return f"{title}: processing did not finish: {exc.message}"
            except Exception as exc:  # noqa: BLE001 - reported per source
                return f"{title}: processing did not finish: {type(exc).__name__}: {exc}"
            finally:
                steps.tick()

        pending = list(new_ids.items())
        for (title, _sid), error in zip(pending, _run_pool(wait, pending), strict=True):
            if error:
                failed.append(error)
                new_ids.pop(title)
        if new_ids:
            # NotebookLM can reset a title to the file name once processing ends.
            titles_by_id = {str(s["id"]): str(s.get("title", "")) for s in self.client.list_sources(nb_id)}
            for title, source_id in list(new_ids.items()):
                if titles_by_id.get(source_id) != title:
                    try:
                        self.client.rename_source(nb_id, source_id, title)
                    except AlchemyError as exc:
                        failed.append(f"{title}: could not set the title: {exc.message}")

        # An old copy goes only when its book's new copy is in, or when this build has no such book.
        # A book that failed to upload keeps its old copy.
        uploaded = {normalize_title(t) for t in new_ids}
        added = set(new_ids.values())
        kept_old: dict[str, str] = {}
        replaced = pruned = 0
        for old in ours:
            key = normalize_title(str(old.get("title", "")))
            if str(old["id"]) in added:
                continue
            if key in book_titles and key not in uploaded:
                if str(old["id"]) not in freed:
                    kept_old[str(old.get("title", ""))] = str(old["id"])
                continue
            if str(old["id"]) not in freed:
                try:
                    self.client.delete_source(nb_id, str(old["id"]))
                except AlchemyError as exc:
                    failed.append(f"{old.get('title', old['id'])}: old copy not removed: {exc.message}")
                    continue
            replaced += key in uploaded
            pruned += key not in book_titles

        if new_ids:  # final read-back: each uploaded book is there once, under its title
            present = {
                str(s["id"]): normalize_title(str(s.get("title", "")))
                for s in self.client.list_sources(nb_id)
            }
            failed.extend(
                f"{title}: not found in the notebook after uploading"
                for title, source_id in new_ids.items()
                if present.get(source_id) != normalize_title(title)
            )
        if failed:
            # The record keeps the old ids too, so the next run still knows which sources are its own.
            self._write_record(nb_id, {**record["ids"], **new_ids})
            raise SyncError(
                f"{len(failed)} problems while uploading {len(books)} books.",
                failed=failed,
                hint="Fix the cause above and run the same command again; it replaces, not duplicates.",
            )
        self._write_record(nb_id, {**kept_old, **new_ids})
        return SyncResult(nb_id, created, len(new_ids), replaced, pruned, limit, freed_first)
