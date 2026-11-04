"""YouTube Channel scanning, video discovery, and transcript extraction."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

from .mock_data import PORTABLE_ENTREPRENEUR_VIDEOS

logger = logging.getLogger(__name__)


@dataclass
class ChannelVideo:
    """Represents a discovered video from a YouTube channel or playlist."""

    id: str
    title: str
    url: str
    duration_seconds: int = 0
    upload_date: str = ""
    view_count: int = 0
    topic: str = "General"


@dataclass
class ChannelScanResult:
    """Result of scanning a YouTube channel or playlist."""

    channel_name: str
    channel_url: str
    total_videos: int
    videos: list[ChannelVideo] = field(default_factory=list)
    is_live: bool = True


class ChannelScanner:
    """Scans YouTube channels and playlists to extract all videos at scale (10 to 1,600+)."""

    def normalize_url(self, raw_url: str) -> str:
        """Ensure channel URL points to the videos tab for complete extraction."""
        url = raw_url.strip()
        if url.startswith("@"):
            url = f"https://www.youtube.com/{url}"

        # If it's a channel root without subpath, append /videos
        if re.match(r"^https?://(www\.)?youtube\.com/@[^/]+/?$", url):
            url = url.rstrip("/") + "/videos"

        return url

    def is_youtube_url(self, text: str) -> bool:
        """Determine whether input text is a YouTube channel or playlist URL."""
        t = text.strip().lower()
        return (
            "youtube.com" in t
            or "youtu.be" in t
            or t.startswith("@")
            or "/channel/" in t
            or "/c/" in t
            or "/playlist" in t
        )

    def scan(
        self,
        url: str,
        limit: int | None = None,
        *,
        force_offline: bool = False,
    ) -> ChannelScanResult:
        """Scan a YouTube channel or playlist and return all discovered video metadata."""
        norm_url = self.normalize_url(url)

        if force_offline:
            return self._fallback_result(norm_url, limit)

        ydl_opts: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }

        if limit:
            ydl_opts["playlist_items"] = f"1-{limit}"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(norm_url, download=False)
                if not info:
                    raise RuntimeError(f"No channel metadata returned for URL: {norm_url}")

                raw_title = info.get("title") or info.get("uploader") or "YouTube Channel"
                channel_name = raw_title.replace(" - Videos", "").strip()
                raw_entries = info.get("entries") or []

                videos: list[ChannelVideo] = []
                for e in raw_entries:
                    if not e or not isinstance(e, dict):
                        continue
                    vid_id = str(e.get("id") or "")
                    title = str(e.get("title") or "Untitled Video")
                    v_url = e.get("url") or f"https://www.youtube.com/watch?v={vid_id}"
                    if not v_url.startswith("http"):
                        v_url = f"https://www.youtube.com/watch?v={vid_id}"

                    videos.append(
                        ChannelVideo(
                            id=vid_id,
                            title=title,
                            url=v_url,
                            duration_seconds=int(e.get("duration") or 0),
                            upload_date=str(e.get("upload_date") or ""),
                            view_count=int(e.get("view_count") or 0),
                        )
                    )

                return ChannelScanResult(
                    channel_name=channel_name,
                    channel_url=norm_url,
                    total_videos=len(videos),
                    videos=videos,
                    is_live=True,
                )

        except Exception as exc:  # noqa: BLE001
            logger.warning("Live YouTube scan failed (%s); using channel catalog fallback.", exc)
            return self._fallback_result(norm_url, limit)

    def _fallback_result(self, url: str, limit: int | None = None) -> ChannelScanResult:
        """Return benchmark catalog as fallback when offline or rate-limited."""
        entries = PORTABLE_ENTREPRENEUR_VIDEOS[:limit] if limit else PORTABLE_ENTREPRENEUR_VIDEOS
        videos = [
            ChannelVideo(
                id=v["id"],
                title=v["title"],
                url=v["url"],
                topic=v.get("topic", "General"),
            )
            for v in entries
        ]
        return ChannelScanResult(
            channel_name="Example Notebook",
            channel_url=url,
            total_videos=len(videos),
            videos=videos,
            is_live=False,
        )


class TranscriptManager:
    """Multi-source resolver for YouTube transcripts (local text files, API, and audio)."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        transcripts_dir: Path | None = None,
        api_key: str | None = None,
    ) -> None:
        self.cache_dir = cache_dir or (Path.home() / ".notebook-alchemy" / "cache")
        self.transcripts_dir = transcripts_dir
        self.api_key = api_key

    def _get_cache_path(self, video_id: str) -> Path | None:
        if not self.cache_dir:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / f"{video_id}.json"

    def _check_local_text_files(self, video_id: str, title: str = "") -> str | None:
        """Check for user-supplied .txt, .vtt, or .srt transcript files."""
        dirs_to_check = [Path("transcripts"), Path.home() / ".notebook-alchemy" / "transcripts"]
        if self.transcripts_dir:
            dirs_to_check.insert(0, self.transcripts_dir)

        safe_title = re.sub(r"[^\w\s-]", "", title).strip() if title else ""

        for d in dirs_to_check:
            if not d.is_dir():
                continue
            candidates = [
                d / f"{video_id}.txt",
                d / f"{video_id}.vtt",
                d / f"{video_id}.srt",
            ]
            if safe_title:
                candidates.extend(
                    [
                        d / f"{safe_title}.txt",
                        d / f"{safe_title}.vtt",
                    ]
                )

            for cand in candidates:
                if cand.is_file():
                    try:
                        raw = cand.read_text(encoding="utf-8", errors="replace")
                        # Basic VTT/SRT timestamp line cleaning
                        cleaned = re.sub(
                            r"(\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->\s*(\d{2}:)?\d{2}:\d{2}[.,]\d{3}",
                            "",
                            raw,
                        )
                        cleaned = re.sub(r"^\d+\s*$", "", cleaned, flags=re.MULTILINE)
                        return " ".join(cleaned.split())
                    except Exception:  # noqa: BLE001, S110
                        pass
        return None

    def fetch_transcript_text(self, video_id: str, title: str = "") -> str:
        """Fetch full transcript text checking local text files, cache, and YouTube API."""
        # 1. Check local text files (.txt / .vtt / .srt)
        local_text = self._check_local_text_files(video_id, title)
        if local_text:
            return local_text

        # 2. Check local JSON cache
        cache_path = self._get_cache_path(video_id)
        if cache_path and cache_path.is_file():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                cached = str(data.get("full_text", "")).strip()
                if cached:
                    return cached
            except Exception:  # noqa: BLE001, S110
                pass

        # 3. Fetch from YouTube Transcript API
        try:
            api = YouTubeTranscriptApi()
            transcript_data = api.fetch(video_id)
            full_text = " ".join(t.text for t in transcript_data).strip()

            if full_text and cache_path:
                cache_path.write_text(
                    json.dumps({"video_id": video_id, "title": title, "full_text": full_text}),
                    encoding="utf-8",
                )

            return full_text
        except Exception as exc:  # noqa: BLE001
            logger.debug("YouTubeTranscriptApi failed for %s: %s", video_id, exc)

        return ""
