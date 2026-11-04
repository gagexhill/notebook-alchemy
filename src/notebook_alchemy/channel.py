"""YouTube channel scanning, video discovery, and transcript resolution."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from youtube_transcript_api import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

from .errors import ChannelScanError
from .mock_data import MOCK_NOTEBOOK_TITLE, SAMPLE_VIDEOS

logger = logging.getLogger(__name__)

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
_HANDLE_RE = re.compile(r"^@[\w.\-]+$")
_CHANNEL_ROOT_RE = re.compile(r"^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PREFERRED_LANGUAGES = ("en", "en-US", "en-GB")
NEGATIVE_CACHE_DAYS = 7


@dataclass
class ChannelVideo:
    """A discovered video from a YouTube channel or playlist."""

    id: str
    title: str
    url: str
    duration_seconds: int = 0
    upload_date: str = ""
    view_count: int = 0
    topic: str = "General"


@dataclass
class ChannelScanResult:
    """Result of scanning a YouTube channel, playlist or video."""

    channel_name: str
    channel_url: str
    total_videos: int
    videos: list[ChannelVideo] = field(default_factory=list)
    is_live: bool = True


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
        use_mock: bool = False,
        _depth: int = 0,
    ) -> ChannelScanResult:
        """Return video metadata, or raise ChannelScanError. Never substitutes sample data."""
        if limit is not None and limit < 1:
            raise ChannelScanError(
                f"--limit must be 1 or more, got {limit}.", hint="Omit --limit to scan everything."
            )
        norm_url = self.normalize_url(url)
        if use_mock:
            return self._mock_result(norm_url, limit)

        logger_sink = _YdlLogger()
        ydl_opts: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "logger": logger_sink,
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
                return self.scan(videos_tab, limit, _depth=1)

        if not videos:
            raise ChannelScanError(
                f"No videos found at '{url}'.",
                hint="Use a channel (@handle), a playlist, or a video URL that has public videos.",
            )

        raw_title = info.get("title") or info.get("channel") or info.get("uploader") or "YouTube"
        channel_name = str(raw_title).replace(" - Videos", "").strip() or "YouTube"
        return ChannelScanResult(
            channel_name=channel_name,
            channel_url=norm_url,
            total_videos=len(videos),
            videos=videos,
            is_live=True,
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
                    upload_date=str(e.get("upload_date") or ""),
                    view_count=int(e.get("view_count") or 0),
                )
            )
        return videos, tab_urls

    @staticmethod
    def _mock_result(url: str, limit: int | None) -> ChannelScanResult:
        entries = SAMPLE_VIDEOS[:limit] if limit else SAMPLE_VIDEOS
        videos = [
            ChannelVideo(id=v["id"], title=v["title"], url=v["url"], topic=v.get("topic", "General"))
            for v in entries
        ]
        return ChannelScanResult(
            channel_name=MOCK_NOTEBOOK_TITLE,
            channel_url=url,
            total_videos=len(videos),
            videos=videos,
            is_live=False,
        )


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
    source: str = "none"  # local | cache | youtube | unavailable | blocked | skipped
    detail: str = ""


class TranscriptManager:
    """Resolves transcripts from local files, the cache, then the YouTube captions API."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        transcripts_dir: Path | None = None,
        *,
        max_workers: int = 4,
        api_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_dir = cache_dir or (Path.home() / ".notebook-alchemy" / "cache")
        self.transcripts_dir = transcripts_dir
        self.max_workers = max(1, max_workers)
        self._api_factory = api_factory or YouTubeTranscriptApi
        self._sleep = sleep

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
            return TranscriptResult(video_id, text, "cache")
        if data.get("unavailable"):
            try:
                checked = datetime.fromisoformat(str(data.get("checked_at", "")))
            except ValueError:
                return None
            if datetime.now(timezone.utc) - checked < timedelta(days=NEGATIVE_CACHE_DAYS):
                return TranscriptResult(video_id, "", "unavailable", str(data.get("reason", "no captions")))
        return None

    def _write_cache(self, video_id: str, title: str, text: str, *, reason: str = "") -> None:
        path = self._cache_path(video_id)
        if path is None:
            return
        record: dict[str, Any] = {"video_id": video_id, "title": title, "full_text": text}
        if not text:
            record.update(
                unavailable=True,
                reason=reason,
                checked_at=datetime.now(timezone.utc).isoformat(),
            )
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
        if not video_id.strip() or video_id.startswith("mock_"):
            return TranscriptResult(video_id, "", "skipped", "no real video id")
        cached = self._read_cache(video_id)
        if cached:
            return cached
        return self._fetch_remote(video_id, title)

    def _fetch_remote(self, video_id: str, title: str) -> TranscriptResult:
        api = self._api_factory()
        permanent = (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                try:
                    fetched = api.fetch(video_id, languages=list(PREFERRED_LANGUAGES))
                except NoTranscriptFound:
                    fetched = next(iter(api.list(video_id))).fetch()
                text = " ".join(" ".join(s.text for s in fetched).split())
                if not text:
                    raise NoTranscriptFound(video_id, PREFERRED_LANGUAGES, None)
                self._write_cache(video_id, title, text)
                return TranscriptResult(video_id, text, "youtube")
            except (RequestBlocked, IpBlocked) as exc:
                return TranscriptResult(
                    video_id, "", "blocked", f"YouTube blocked the request ({type(exc).__name__})"
                )
            except permanent as exc:
                reason = type(exc).__name__
                self._write_cache(video_id, title, "", reason=reason)
                return TranscriptResult(video_id, "", "unavailable", reason)
            except StopIteration:
                self._write_cache(video_id, title, "", reason="NoTranscriptFound")
                return TranscriptResult(video_id, "", "unavailable", "NoTranscriptFound")
            except Exception as exc:  # noqa: BLE001 - library raises varied transient errors
                last_exc = exc
                logger.debug("Transcript fetch for %s failed (attempt %d): %s", video_id, attempt + 1, exc)
                if attempt < 2:
                    self._sleep(2**attempt)
        return TranscriptResult(video_id, "", "unavailable", f"fetch failed: {type(last_exc).__name__}")

    def fetch_transcript_text(self, video_id: str, title: str = "") -> str:
        """Return transcript text, or an empty string when none is available."""
        return self.resolve(video_id, title).text

    def fetch_many(
        self,
        items: list[tuple[str, str]],
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, TranscriptResult]:
        """Resolve many (video_id, title) pairs with a bounded worker pool."""
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
        return results
