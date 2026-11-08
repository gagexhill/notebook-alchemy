"""YouTube channel scanning, video discovery, and transcript resolution."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ChannelScanError

logger = logging.getLogger(__name__)

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
_HANDLE_RE = re.compile(r"^@[\w.\-]+$")
_CHANNEL_ROOT_RE = re.compile(r"^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Caption languages to try first, in order; ALCHEMY_CAPTION_LANGUAGES overrides them. When none
# exists, a caption track the uploader wrote is preferred over an automatic one in any language.
PREFERRED_LANGUAGES = ("en", "en-US", "en-GB")
NEGATIVE_CACHE_DAYS = 7
# Channel tabs a scan can read. Videos and live streams have captions worth reading; Shorts are
# usually under a minute, so they are left out unless asked for.
CHANNEL_TABS = ("videos", "streams", "shorts")
DEFAULT_TABS = ("videos", "streams")
PO_TOKEN_STOP_AFTER = 2  # proof-of-origin demands before the run stops asking YouTube for captions
_YOUTUBE_ID_RE = re.compile(r"^[\w-]{11}$")


@dataclass
class ChannelVideo:
    """A discovered video from a YouTube channel or playlist."""

    id: str
    title: str
    url: str
    duration_seconds: int = 0
    published: str = ""  # YYYY-MM-DD; from a channel listing it is approximate ("3 years ago")


@dataclass
class ChannelScanResult:
    """Result of scanning a YouTube channel, playlist or video."""

    channel_name: str
    videos: list[ChannelVideo] = field(default_factory=list)


class _YdlLogger:
    """Collects yt-dlp messages so nothing is printed behind the CLI's back."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        self.errors.append(_ANSI_RE.sub("", msg).removeprefix("ERROR: ").strip())


def video_id_from_url(url: str) -> str:
    """A stable id for a video URL: the YouTube id when present, else a short URL hash."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    match = None
    if host == "youtu.be":
        match = re.match(r"^/([\w\-]{6,})", parts.path)
    elif host in YOUTUBE_HOSTS:
        match = re.search(r"(?:^|&)v=([\w\-]{6,})", parts.query) or re.match(
            r"^/(?:shorts|embed|live)/([\w\-]{6,})", parts.path
        )
    if match:
        return match.group(1)
    return "url_" + hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:10]


class ChannelScanner:
    """Scans YouTube channels, playlists and single videos for flat video metadata."""

    def normalize_url(self, raw_url: str) -> str:
        """Resolve a channel root to its Videos tab; leave playlists and videos alone."""
        url = raw_url.strip()
        if _HANDLE_RE.match(url):
            url = f"https://www.youtube.com/{url}"
        elif "://" not in url and " " not in url and "." in url.split("/")[0]:
            url = f"https://{url}"

        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host in YOUTUBE_HOSTS and _CHANNEL_ROOT_RE.match(parts.path):
            # Share links (?si=...) and tracking parameters are dropped with the root.
            return f"https://www.youtube.com{parts.path.rstrip('/')}/videos"
        return url

    def channel_root(self, raw_url: str) -> str | None:
        """`https://www.youtube.com/@handle` (or /channel/..., /c/..., /user/...) for a channel URL."""
        url = self.normalize_url(raw_url)
        parts = urlsplit(url)
        if (parts.hostname or "").lower() not in YOUTUBE_HOSTS:
            return None
        path = parts.path.rstrip("/")
        if path.endswith("/videos") and _CHANNEL_ROOT_RE.match(path[: -len("/videos")]):
            return f"https://www.youtube.com{path[: -len('/videos')]}"
        return None

    def is_youtube_url(self, text: str) -> bool:
        """True for YouTube URLs and bare @handles; false for free text that mentions YouTube."""
        value = text.strip()
        if _HANDLE_RE.match(value):
            return True
        if not value or " " in value:
            return False
        candidate = value if "://" in value else f"https://{value}"
        host = (urlsplit(candidate).hostname or "").lower()
        return host in YOUTUBE_HOSTS or host == "youtu.be"

    def scan(
        self,
        url: str,
        limit: int | None = None,
        *,
        tabs: tuple[str, ...] = DEFAULT_TABS,
    ) -> ChannelScanResult:
        """Return video metadata, or raise ChannelScanError. Never substitutes sample data.

        A channel URL is read tab by tab (`tabs`, in order, a tab the channel does not have is
        skipped); a playlist or video URL is read as it is.
        """
        if limit is not None and limit < 1:
            raise ChannelScanError(
                f"--limit must be 1 or more, got {limit}.", hint="Omit --limit to scan everything."
            )
        unknown = [t for t in tabs if t not in CHANNEL_TABS]
        if unknown or not tabs:
            raise ChannelScanError(
                f"Unknown channel tab: {', '.join(unknown) or '(none)'}.",
                hint=f"Use one or more of: {', '.join(CHANNEL_TABS)}.",
            )
        root = self.channel_root(url)
        if root is None:
            return self._scan_one(url, limit)

        name = ""
        videos: list[ChannelVideo] = []
        seen: set[str] = set()
        for index, tab in enumerate(dict.fromkeys(tabs)):  # each tab once, in the order given
            try:
                result = self._scan_one(f"{root}/{tab}", limit)
            except ChannelScanError as exc:
                missing = f"does not have a {tab} tab" in exc.message or "No videos found" in exc.message
                if missing or index > 0:
                    # A tab the channel lacks is normal. A later tab failing after the first one
                    # worked is not worth losing the scan over, so it is skipped and logged.
                    logger.info("Skipped the %s tab of %s: %s", tab, root, exc.message)
                    continue
                raise
            name = name or result.channel_name
            for video in result.videos:
                if video.id not in seen:
                    seen.add(video.id)
                    videos.append(video)
        if not videos:
            raise ChannelScanError(
                f"No videos found at '{url}' (tabs: {', '.join(tabs)}).",
                hint="Use a channel (@handle), a playlist, or a video URL that has public videos.",
            )
        return ChannelScanResult(channel_name=name, videos=videos[:limit] if limit else videos)

    def _scan_one(self, url: str, limit: int | None, _depth: int = 0) -> ChannelScanResult:
        """Read one channel tab, playlist or video."""
        norm_url = self.normalize_url(url)
        import yt_dlp  # imported on use: it is slow to import and most commands never scan

        logger_sink = _YdlLogger()
        ydl_opts: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "logger": logger_sink,
            # Turns a listing's "3 years ago" into a timestamp, so every video carries a date.
            "extractor_args": {"youtubetab": {"approximate_date": [""]}},
        }
        if limit:
            ydl_opts["playlist_items"] = f"1-{limit}"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(norm_url, download=False)
        except Exception as exc:
            detail = logger_sink.errors[-1] if logger_sink.errors else str(exc)
            raise ChannelScanError(
                f"YouTube could not load '{url}': {_ANSI_RE.sub('', detail).strip()}",
                hint="Check the URL and your network connection, then try again.",
            ) from exc
        if not info:
            detail = logger_sink.errors[-1] if logger_sink.errors else "no data returned"
            raise ChannelScanError(
                f"YouTube returned nothing for '{url}': {detail}",
                hint="Check that the channel or playlist exists and is public.",
            )

        entries = info.get("entries")
        if entries is None:  # a single video URL
            raw_entries: list[Any] = [info]
        else:
            raw_entries = list(entries)

        videos, tab_urls = self._collect_videos(raw_entries)

        if not videos and tab_urls and _depth == 0:
            videos_tab = next((u for u in tab_urls if u.rstrip("/").endswith("/videos")), None)
            if videos_tab:
                return self._scan_one(videos_tab, limit, _depth=1)

        if not videos:
            raise ChannelScanError(
                f"No videos found at '{url}'.",
                hint="Use a channel (@handle), a playlist, or a video URL that has public videos.",
            )

        raw_title = info.get("title") or info.get("channel") or info.get("uploader") or "YouTube"
        channel_name = re.sub(r" - (Videos|Live|Shorts)$", "", str(raw_title)).strip() or "YouTube"
        return ChannelScanResult(
            channel_name=channel_name,
            videos=videos,
        )

    @staticmethod
    def _collect_videos(raw_entries: Iterable[Any]) -> tuple[list[ChannelVideo], list[str]]:
        """Split flat entries into real videos and channel tab/playlist entries."""
        videos: list[ChannelVideo] = []
        tab_urls: list[str] = []
        seen: set[str] = set()
        for e in raw_entries:
            if not isinstance(e, dict):
                continue
            if e.get("ie_key") == "YoutubeTab" or e.get("_type") == "playlist":
                if e.get("url"):
                    tab_urls.append(str(e["url"]))
                continue
            vid_id = str(e.get("id") or "")
            if not vid_id or vid_id in seen:
                continue
            seen.add(vid_id)
            v_url = str(e.get("url") or e.get("webpage_url") or "")
            if not v_url.startswith("http"):
                v_url = f"https://www.youtube.com/watch?v={vid_id}"
            videos.append(
                ChannelVideo(
                    id=vid_id,
                    title=str(e.get("title") or "Untitled Video"),
                    url=v_url,
                    duration_seconds=int(e.get("duration") or 0),
                    published=_entry_date(e),
                )
            )
        return videos, tab_urls


def _entry_date(entry: dict[str, Any]) -> str:
    """YYYY-MM-DD from a yt-dlp entry (upload_date, else a timestamp), or "" when it has none."""
    raw = str(entry.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    stamp = entry.get("timestamp") or entry.get("release_timestamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        return datetime.fromtimestamp(stamp, UTC).date().isoformat()
    return ""


# --- Transcripts -----------------------------------------------------------------------------

_TIMING_RE = re.compile(r"^\s*(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->\s*(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}.*$")
_TAG_RE = re.compile(r"<[^>]+>")
_HEADER_RE = re.compile(r"^(?:Kind|Language|X-TIMESTAMP-MAP)\s*:", re.IGNORECASE)


def parse_captions(raw: str) -> str:
    """Turn WebVTT or SRT text into plain spoken text (headers, cues and tags removed)."""
    lines: list[str] = []
    in_block = False
    for line in raw.replace("﻿", "").splitlines():
        s = line.strip()
        if not s:
            in_block = False
            continue
        if s.startswith("WEBVTT") or _HEADER_RE.match(s):
            continue
        if s.startswith("NOTE") or s in ("STYLE", "REGION"):
            in_block = True
            continue
        if in_block or _TIMING_RE.match(s) or s.isdigit():
            continue
        s = html.unescape(_TAG_RE.sub("", s)).strip()
        if not s or (lines and s == lines[-1]):
            continue
        if lines and s.startswith(lines[-1]):
            lines[-1] = s  # rolling captions repeat the previous line, extended
            continue
        lines.append(s)
    return " ".join(lines)


@dataclass
class TranscriptResult:
    """Outcome of resolving one transcript. `text` is empty unless a source produced it."""

    video_id: str
    text: str = ""
    source: str = "none"  # local | cache | youtube | subtitles | unavailable | blocked | skipped
    detail: str = ""
    published: str = ""  # YYYY-MM-DD when a source reported the exact upload date
    method: str = ""  # for cached words: how they were first got ("gemini-video" when Gemini watched)


class SubtitlesBlocked(Exception):
    """YouTube refused yt-dlp's request from this network (a bot check, a rate limit)."""


class SubtitlesUnavailable(Exception):
    """The video has no subtitle track yt-dlp can read, or the video itself cannot be read."""


class SubtitlesSetupError(Exception):
    """yt-dlp cannot run as configured (for example it cannot read the browser's cookies)."""


# yt-dlp error text that means "this network is being refused", not "this video has no captions".
_BLOCK_MARKERS = ("sign in to confirm", "not a bot", "http error 429", "too many requests", "po token")
_GONE_MARKERS = (
    "private video",
    "video unavailable",
    "has been removed",
    "members-only",
    "age-restricted",
    "confirm your age",
    "inappropriate for some users",
)
# yt-dlp could not read the browser's sign-in: a setup problem for every video, not this one.
_COOKIE_MARKERS = ("cookie", "dpapi")


class YtDlpSubtitles:
    """Captions through yt-dlp: a second route to YouTube's subtitle tracks, with the exact date.

    It meets the same network blocks as the captions API; with `cookies_browser` (your own signed-in
    browser, read by yt-dlp in memory and never stored) it often gets through where the API does not.
    """

    def __init__(
        self,
        languages: tuple[str, ...] = PREFERRED_LANGUAGES,
        *,
        cookies_browser: str | None = None,
        ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.languages = tuple(languages) or PREFERRED_LANGUAGES
        self.cookies_browser = cookies_browser
        self._ydl_factory = ydl_factory

    def fetch(self, video_id: str) -> tuple[str, str]:
        """(text, YYYY-MM-DD upload date). Raises SubtitlesBlocked or SubtitlesUnavailable."""
        sink = _YdlLogger()
        opts: dict[str, Any] = {"skip_download": True, "quiet": True, "no_warnings": True, "logger": sink}
        if self.cookies_browser:
            opts["cookiesfrombrowser"] = (self.cookies_browser,)
        if self._ydl_factory is None:
            import yt_dlp  # imported on use, like the scanner

            factory: Callable[[dict[str, Any]], Any] = yt_dlp.YoutubeDL
        else:
            factory = self._ydl_factory
        url = f"https://www.youtube.com/watch?v={video_id}"
        with factory(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False) or {}
            except Exception as exc:
                message = (sink.errors[-1] if sink.errors else str(exc)).lower()
                if self.cookies_browser and any(m in message for m in _COOKIE_MARKERS):
                    raise SubtitlesSetupError(
                        f"yt-dlp could not read the {self.cookies_browser} sign-in "
                        "(on Windows, Chrome and Edge often lock or encrypt it)"
                    ) from exc
                if any(m in message for m in _BLOCK_MARKERS):
                    raise SubtitlesBlocked(message[:200]) from exc
                if any(m in message for m in _GONE_MARKERS):
                    raise SubtitlesUnavailable(message[:200]) from exc
                raise
            track = self._pick(info)
            if track is None:
                raise SubtitlesUnavailable("no subtitle track")
            try:
                raw = ydl.urlopen(track["url"]).read().decode("utf-8", errors="replace")
            except Exception as exc:
                message = str(exc).lower()
                if any(m in message for m in _BLOCK_MARKERS):
                    raise SubtitlesBlocked(message[:200]) from exc
                raise
        text = " ".join(parse_captions(raw).split())
        if not text:
            raise SubtitlesUnavailable("empty subtitle track")
        return text, _entry_date(info)

    def _pick(self, info: dict[str, Any]) -> dict[str, Any] | None:
        """A WebVTT track: a written one in a preferred language, then an automatic one, then any."""
        written = info.get("subtitles") or {}
        automatic = info.get("automatic_captions") or {}
        for tracks in (written, automatic):
            for lang in self.languages:
                vtt = self._vtt(tracks.get(lang))
                if vtt:
                    return vtt
        for tracks in (written, automatic):
            for formats in tracks.values():
                vtt = self._vtt(formats)
                if vtt:
                    return vtt
        return None

    @staticmethod
    def _vtt(formats: Any) -> dict[str, Any] | None:
        for fmt in formats or []:
            if isinstance(fmt, dict) and fmt.get("ext") == "vtt" and fmt.get("url"):
                return fmt
        return None


# Reasons the captions API gives that hold for any route to the same video; others may differ by route.
_FINAL_REASONS = frozenset({"TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable", "InvalidVideoId"})


class _Pacer:
    """Spaces remote caption requests: one at a time across all workers, a random gap apart."""

    def __init__(
        self,
        spacing: tuple[float, float],
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        rand: Callable[[float, float], float],
    ) -> None:
        self.spacing, self._sleep, self._clock, self._rand = spacing, sleep, clock, rand
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next_at)
            low, high = self.spacing
            self._next_at = start + (self._rand(low, high) if high > 0 else 0.0)
        if start > now:
            self._sleep(start - now)


class TranscriptManager:
    """Resolves transcripts: local files, the cache, the captions API, then yt-dlp subtitles.

    A block pauses only the route that was refused, for `block_pause` seconds; the run carries on
    through the other routes, and `fetch_many` tries blocked videos once more when a route reopens.
    Remote requests are spaced by `spacing` seconds so a long channel is not refused in the first
    place. A block is never cached, so the next run tries the video again.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        transcripts_dir: Path | None = None,
        *,
        max_workers: int = 4,
        api_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        languages: tuple[str, ...] = PREFERRED_LANGUAGES,
        subtitles: YtDlpSubtitles | None = None,
        bulk: Callable[[list[tuple[str, str]]], dict[str, str]] | None = None,
        spacing: tuple[float, float] = (0.0, 0.0),
        block_pause: float = 180.0,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.cache_dir = cache_dir or (Path.home() / ".notebook-alchemy" / "cache")
        self.transcripts_dir = transcripts_dir
        self.max_workers = max(1, max_workers)
        self._api_factory = api_factory
        self._sleep = sleep
        self._clock = clock
        self.languages = tuple(languages) or PREFERRED_LANGUAGES
        self.subtitles = subtitles
        # A route that reads many videos at once (NotebookLM fetching the transcripts on Google's
        # side), used for videos the per-video routes could not read.
        self.bulk = bulk
        self.block_pause = block_pause
        self.notices: list[str] = []  # route problems to tell the user once
        self._pacer = _Pacer(spacing, sleep, clock, rand)
        # Per route: the clock time until which it is paused after a block (0 = open).
        self._paused_until: dict[str, float] = {}
        self._pause_lock = threading.Lock()
        self._route_locks = {"api": threading.Lock(), "subtitles": threading.Lock()}
        self._po_token_demands = 0
        self.blocked_total = 0  # videos this run could not get captions for because YouTube blocked it
        self.by_source: dict[str, int] = {}  # how this run's videos were resolved, for the summary

    # -- helpers --

    @staticmethod
    def _safe_id(video_id: str) -> str:
        return re.sub(r"[^\w\-]", "_", video_id.strip())

    def _cache_path(self, video_id: str) -> Path | None:
        safe = self._safe_id(video_id)
        return self.cache_dir / f"{safe}.json" if safe else None

    def _local_dirs(self) -> list[Path]:
        dirs = [Path("transcripts"), Path.home() / ".notebook-alchemy" / "transcripts"]
        if self.transcripts_dir:
            dirs.insert(0, self.transcripts_dir)
        return dirs

    def _check_local_text_files(self, video_id: str, title: str = "") -> str | None:
        """Read a user-supplied .txt, .vtt or .srt transcript matched by id or title."""
        safe_title = re.sub(r"[^\w\s-]", "", title).strip() if title else ""
        stems = [self._safe_id(video_id)] if video_id.strip() else []
        if safe_title:
            stems.append(safe_title)
        for directory in self._local_dirs():
            if not directory.is_dir():
                continue
            for stem in stems:
                for suffix in (".txt", ".vtt", ".srt"):
                    candidate = directory / f"{stem}{suffix}"
                    if not candidate.is_file():
                        continue
                    try:
                        raw = candidate.read_text(encoding="utf-8", errors="replace")
                    except OSError as exc:
                        logger.warning("Could not read transcript file %s: %s", candidate, exc)
                        continue
                    text = " ".join(raw.split()) if suffix == ".txt" else parse_captions(raw)
                    if text:
                        return text
        return None

    def _pause(self, route: str) -> None:
        with self._pause_lock:
            self._paused_until[route] = self._clock() + self.block_pause

    def _is_paused(self, route: str) -> bool:
        with self._pause_lock:
            return self._paused_until.get(route, 0.0) > self._clock()

    def next_reopening(self) -> float | None:
        """Seconds until the first paused route reopens, or None when none is paused."""
        with self._pause_lock:
            ends = [t for t in self._paused_until.values() if t > self._clock()]
        return min(ends) - self._clock() if ends else None

    # -- cache --

    def _read_cache(self, video_id: str) -> TranscriptResult | None:
        path = self._cache_path(video_id)
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable transcript cache %s: %s", path, exc)
            return None
        text = str(data.get("full_text", "")).strip()
        if text:
            return TranscriptResult(
                video_id,
                text,
                "cache",
                published=str(data.get("published", "")),
                method=str(data.get("method", "")),
            )
        if data.get("unavailable"):
            try:
                checked = datetime.fromisoformat(str(data.get("checked_at", "")))
            except ValueError:
                return None
            if datetime.now(UTC) - checked < timedelta(days=NEGATIVE_CACHE_DAYS):
                return TranscriptResult(video_id, "", "unavailable", str(data.get("reason", "no captions")))
        return None

    def _write_cache(
        self, video_id: str, title: str, text: str, *, reason: str = "", method: str = "", published: str = ""
    ) -> None:
        path = self._cache_path(video_id)
        if path is None:
            return
        record: dict[str, Any] = {"video_id": video_id, "title": title, "full_text": text}
        if text:
            record.update(method=method, published=published)
        else:
            record.update(unavailable=True, reason=reason, checked_at=datetime.now(UTC).isoformat())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write transcript cache %s: %s", path, exc)

    # -- resolution --

    def resolve(self, video_id: str, title: str = "") -> TranscriptResult:
        """Resolve one transcript; never raises for an unavailable one."""
        local = self._check_local_text_files(video_id, title)
        if local:
            return TranscriptResult(video_id, local, "local")
        if not _YOUTUBE_ID_RE.fullmatch(video_id.strip()):
            return TranscriptResult(video_id, "", "skipped", "no YouTube video id")
        cached = self._read_cache(video_id)
        if cached:
            return cached
        return self._fetch_remote(video_id, title)

    def _fetch_remote(self, video_id: str, title: str) -> TranscriptResult:
        """Each open route in turn. A final "no captions" is cached; a block is not."""
        routes: list[tuple[str, Callable[[str, str], TranscriptResult]]] = [("api", self._via_api)]
        if self.subtitles is not None:
            routes.append(("subtitles", self._via_subtitles))
        outcome: TranscriptResult | None = None
        for route, fetch in routes:
            # One remote request per route at a time, and the pause is checked under the lock, so a
            # route closed by one worker is not called again by another already waiting.
            with self._route_locks[route]:
                if self._is_paused(route):
                    result = None
                else:
                    self._pacer.wait()
                    result = fetch(video_id, title)
            if result is None:
                outcome = outcome or TranscriptResult(
                    video_id, "", "blocked", f"YouTube blocked an earlier {route} request in this run"
                )
                continue
            if result.text:
                self._write_cache(
                    video_id, title, result.text, method=result.source, published=result.published
                )
                return result
            if result.source == "unavailable" and result.detail in _FINAL_REASONS:
                self._write_cache(video_id, title, "", reason=result.detail)
                return result
            # Blocked, or a reason another route may not share: keep the most telling, try the next.
            if outcome is None or result.source == "blocked":
                outcome = result
        if outcome is None:
            return TranscriptResult(video_id, "", "unavailable", "no route to YouTube")
        if outcome.source == "unavailable" and not outcome.detail.startswith(
            ("fetch failed", "subtitles failed")
        ):
            # Every route said the video has nothing to read (age-restricted, removed, no track):
            # remembered for a week. A network failure is not remembered.
            self._write_cache(video_id, title, "", reason=outcome.detail)
        return outcome

    def _via_api(self, video_id: str, title: str) -> TranscriptResult:
        import youtube_transcript_api as yta  # imported on use, like yt-dlp

        api = (self._api_factory or yta.YouTubeTranscriptApi)()
        # Blocked: this network is refused (an IP block, or a consent page that cannot be passed
        # from here). The route pauses; retrying now would get the same answer.
        blocked = (yta.RequestBlocked, yta.FailedToCreateConsentCookie)
        # Retried: the request or the page YouTube sent back failed.
        transient = (yta.YouTubeRequestFailed, yta.YouTubeDataUnparsable)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                text = self._caption_text(api, video_id, yta.NoTranscriptFound)
                if not text:
                    return TranscriptResult(video_id, "", "unavailable", "TranscriptsDisabled")
                return TranscriptResult(video_id, text, "youtube")
            except blocked as exc:
                self._pause("api")
                return TranscriptResult(
                    video_id, "", "blocked", f"YouTube blocked the request ({type(exc).__name__})"
                )
            except yta.PoTokenRequired as exc:
                # YouTube can demand a proof-of-origin token for some videos only, so the route
                # pauses after a second demand, not the first.
                with self._pause_lock:
                    self._po_token_demands += 1
                    demands = self._po_token_demands
                if demands >= PO_TOKEN_STOP_AFTER:
                    self._pause("api")
                return TranscriptResult(
                    video_id, "", "blocked", f"YouTube blocked the request ({type(exc).__name__})"
                )
            except transient as exc:
                last_exc = exc
            except yta.CouldNotRetrieveTranscript as exc:
                # No captions, disabled, private, age-restricted, removed, invalid id, and any
                # reason a later library version adds.
                return TranscriptResult(video_id, "", "unavailable", type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 - network errors from the HTTP stack vary
                last_exc = exc
            logger.debug("Transcript fetch for %s failed (attempt %d): %s", video_id, attempt + 1, last_exc)
            if attempt < 2:
                self._sleep(2**attempt)
        return TranscriptResult(video_id, "", "unavailable", f"fetch failed: {type(last_exc).__name__}")

    def _via_subtitles(self, video_id: str, title: str) -> TranscriptResult:
        assert self.subtitles is not None  # noqa: S101 - only listed as a route when set
        try:
            text, published = self.subtitles.fetch(video_id)
        except SubtitlesBlocked as exc:
            self._pause("subtitles")
            return TranscriptResult(video_id, "", "blocked", f"YouTube blocked yt-dlp ({exc})")
        except SubtitlesUnavailable as exc:
            return TranscriptResult(video_id, "", "unavailable", f"subtitles: {exc}")
        except SubtitlesSetupError as exc:
            # Every video would fail the same way: the route closes for this run, said once.
            with self._pause_lock:
                self._paused_until["subtitles"] = float("inf")
                if str(exc) not in self.notices:
                    self.notices.append(str(exc))
            return TranscriptResult(video_id, "", "blocked", "yt-dlp route closed")
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many kinds; the reason is kept
            logger.debug("yt-dlp subtitles for %s failed: %s", video_id, exc)
            return TranscriptResult(video_id, "", "unavailable", f"subtitles failed: {type(exc).__name__}")
        return TranscriptResult(video_id, text, "subtitles", published=published)

    def _caption_text(self, api: Any, video_id: str, not_found: type[Exception]) -> str:
        """Captions in a preferred language, else the uploader's own track, else an automatic one."""
        try:
            fetched = api.fetch(video_id, languages=list(self.languages))
        except not_found:
            tracks = list(api.list(video_id))
            if not tracks:
                raise
            tracks.sort(key=lambda tr: bool(getattr(tr, "is_generated", False)))  # written first
            fetched = tracks[0].fetch()
        return " ".join(" ".join(s.text for s in fetched).split())

    @staticmethod
    def _worth_another_route(result: TranscriptResult) -> bool:
        """Blocked, or failed for a reason that is not "this video has no captions"."""
        if result.text or result.source in ("skipped", "local", "cache"):
            return False
        return result.source == "blocked" or result.detail not in _FINAL_REASONS

    def fetch_many(
        self,
        items: list[tuple[str, str]],
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, TranscriptResult]:
        """Resolve many (video_id, title) pairs with a bounded worker pool.

        Videos a block stopped are tried once more after the pass, when a paused route reopens
        within `block_pause` seconds.
        """
        results: dict[str, TranscriptResult] = {}
        total = len(items)
        if not total:
            return results
        done = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for result in pool.map(lambda pair: self.resolve(pair[0], pair[1]), items):
                results[result.video_id] = result
                done += 1
                if progress:
                    progress(done, total)
        missing = [(vid, title) for vid, title in items if self._worth_another_route(results[vid])]
        if missing and self.bulk is not None:
            texts = self.bulk(missing)
            for vid, title in missing:
                text = " ".join(str(texts.get(vid, "")).split())
                if text:
                    self._write_cache(vid, title, text, method="notebooklm")
                    results[vid] = TranscriptResult(vid, text, "notebooklm")
        retry = [(vid, title) for vid, title in items if results[vid].source == "blocked"]
        wait = self.next_reopening()
        if retry and wait is not None and wait <= self.block_pause:
            logger.info("Waiting %.0f s for YouTube, then retrying %d blocked videos.", wait, len(retry))
            self._sleep(wait)
            for vid, title in retry:
                results[vid] = self.resolve(vid, title)
        for result in results.values():
            self.by_source[result.source] = self.by_source.get(result.source, 0) + 1
        self.blocked_total += sum(1 for r in results.values() if r.source == "blocked")
        return results
