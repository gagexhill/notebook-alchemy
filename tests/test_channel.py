"""Channel scanning and transcript resolution."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from youtube_transcript_api import NoTranscriptFound, RequestBlocked, TranscriptsDisabled

from notebook_alchemy import channel
from notebook_alchemy.channel import (
    ChannelScanner,
    TranscriptManager,
    parse_captions,
    video_id_from_url,
)
from notebook_alchemy.errors import ChannelScanError

# --- URL handling ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Hyperplexed", "https://www.youtube.com/@Hyperplexed/videos"),
        ("https://www.youtube.com/@Hyperplexed", "https://www.youtube.com/@Hyperplexed/videos"),
        ("https://www.youtube.com/@Hyperplexed/", "https://www.youtube.com/@Hyperplexed/videos"),
        (
            "https://www.youtube.com/@Hyperplexed/videos",
            "https://www.youtube.com/@Hyperplexed/videos",
        ),
        (
            "https://www.youtube.com/@Hyperplexed?si=abc123",
            "https://www.youtube.com/@Hyperplexed/videos",
        ),
        (
            "https://www.youtube.com/@Hyperplexed/?si=abc",
            "https://www.youtube.com/@Hyperplexed/videos",
        ),
        ("https://m.youtube.com/@Hyperplexed", "https://www.youtube.com/@Hyperplexed/videos"),
        ("youtube.com/@Hyperplexed", "https://www.youtube.com/@Hyperplexed/videos"),
        ("https://www.youtube.com/c/Hyperplexed", "https://www.youtube.com/c/Hyperplexed/videos"),
        (
            "https://www.youtube.com/user/Hyperplexed",
            "https://www.youtube.com/user/Hyperplexed/videos",
        ),
        (
            "https://www.youtube.com/channel/UCmEzz-dPBVrsy4ZluSsYHDg",
            "https://www.youtube.com/channel/UCmEzz-dPBVrsy4ZluSsYHDg/videos",
        ),
        (
            "https://www.youtube.com/@Hyperplexed/shorts",
            "https://www.youtube.com/@Hyperplexed/shorts",
        ),
        (
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/playlist?list=PL123",
        ),
        (
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://www.youtube.com/watch?v=abcdefghijk",
        ),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert ChannelScanner().normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://www.youtube.com/@hubspot", True),
        ("https://youtu.be/abc123xyz", True),
        ("@Portableentrepreneur", True),
        ("youtube.com/@x", True),
        ("Example Notebook", False),
        ("my/c/notes", False),
        ("Learn youtube.com growth", False),
        ("@handle not a url", False),
        ("00000000-0000-4000-8000-000000000000", False),
        ("", False),
    ],
)
def test_is_youtube_url(text: str, expected: bool) -> None:
    assert ChannelScanner().is_youtube_url(text) is expected


def test_video_id_from_url() -> None:
    assert video_id_from_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5") == "dQw4w9WgXcQ"
    assert video_id_from_url("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    first = video_id_from_url("https://example.test/a")
    assert first.startswith("url_") and first == video_id_from_url("https://example.test/a")


# --- scanning -------------------------------------------------------------------------------------


class _FakeYDL:
    """Replaces yt_dlp.YoutubeDL; `script` maps URL -> info dict or an exception."""

    script: ClassVar[dict[str, object]] = {}
    opts_seen: ClassVar[list[dict]] = []

    def __init__(self, opts: dict) -> None:
        self.opts = opts
        _FakeYDL.opts_seen.append(opts)

    def __enter__(self) -> _FakeYDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False):
        result = _FakeYDL.script.get(url)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def ydl(monkeypatch: pytest.MonkeyPatch) -> type[_FakeYDL]:
    _FakeYDL.script = {}
    _FakeYDL.opts_seen = []
    monkeypatch.setattr(channel.yt_dlp, "YoutubeDL", _FakeYDL)
    return _FakeYDL


def _entry(i: int) -> dict:
    return {
        "id": f"vid{i:08d}",
        "title": f"Title {i}",
        "url": f"https://www.youtube.com/watch?v=vid{i:08d}",
        "duration": 60 * i,
    }


def test_scan_returns_real_videos(ydl: type[_FakeYDL]) -> None:
    ydl.script["https://www.youtube.com/@x/videos"] = {
        "title": "X - Videos",
        "entries": [_entry(1), _entry(2), _entry(2), None],
    }
    result = ChannelScanner().scan("@x", limit=5)
    assert result.channel_name == "X"
    assert [v.id for v in result.videos] == ["vid00000001", "vid00000002"]  # duplicate id dropped
    assert result.videos[1].duration_seconds == 120
    assert ydl.opts_seen[0]["playlist_items"] == "1-5"


def test_network_failure_raises_instead_of_returning_sample_data(ydl: type[_FakeYDL]) -> None:
    ydl.script["https://www.youtube.com/@gone/videos"] = RuntimeError("HTTP Error 404: Not Found")
    with pytest.raises(ChannelScanError) as err:
        ChannelScanner().scan("@gone")
    assert "404" in err.value.message and err.value.hint


def test_missing_info_raises(ydl: type[_FakeYDL]) -> None:
    ydl.script["https://www.youtube.com/@none/videos"] = None
    with pytest.raises(ChannelScanError):
        ChannelScanner().scan("@none")


def test_zero_videos_raises(ydl: type[_FakeYDL]) -> None:
    ydl.script["https://www.youtube.com/@empty/videos"] = {"title": "Empty", "entries": []}
    with pytest.raises(ChannelScanError) as err:
        ChannelScanner().scan("@empty")
    assert "No videos found" in err.value.message


@pytest.mark.parametrize("limit", [0, -1, -5])
def test_bad_limit_is_rejected(limit: int) -> None:
    with pytest.raises(ChannelScanError):
        ChannelScanner().scan("@x", limit=limit)


def test_tab_entries_are_never_videos(ydl: type[_FakeYDL]) -> None:
    tabs = {
        "title": "Chan",
        "entries": [
            {
                "id": "UCabc",
                "title": "Chan - Videos",
                "url": "https://www.youtube.com/channel/UCabc/videos",
                "ie_key": "YoutubeTab",
            },
            {
                "id": "UCabc",
                "title": "Chan - Shorts",
                "url": "https://www.youtube.com/channel/UCabc/shorts",
                "ie_key": "YoutubeTab",
            },
        ],
    }
    ydl.script["https://www.youtube.com/@chan/videos"] = tabs
    ydl.script["https://www.youtube.com/channel/UCabc/videos"] = {
        "title": "Chan - Videos",
        "entries": [_entry(7)],
    }
    result = ChannelScanner().scan("@chan")
    assert [v.id for v in result.videos] == ["vid00000007"]


def test_tabs_only_with_no_videos_tab_raises(ydl: type[_FakeYDL]) -> None:
    ydl.script["https://www.youtube.com/@t/videos"] = {
        "entries": [{"id": "UC1", "title": "t - Shorts", "url": "https://x/shorts", "ie_key": "YoutubeTab"}]
    }
    with pytest.raises(ChannelScanError):
        ChannelScanner().scan("@t")


def test_single_video_url(ydl: type[_FakeYDL]) -> None:
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    ydl.script[url] = {"id": "abcdefghijk", "title": "Solo", "webpage_url": url}
    result = ChannelScanner().scan(url)
    assert len(result.videos) == 1 and result.videos[0].title == "Solo"


# --- captions -------------------------------------------------------------------------------------


def test_parse_vtt_strips_headers_cues_tags_and_rolling_duplicates() -> None:
    vtt = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "NOTE a comment\nspanning lines\n\n"
        "1\n00:00:00.000 --> 00:00:02.000 align:start position:0%\n"
        "<c>hello</c><00:00:01.000><c> world</c>\n\n"
        "2\n00:00:02.000 --> 00:00:04.000\nhello world\nhello world again\n\n"
        "00:00:04.000 --> 00:00:05.000\nsecond &amp; last\n"
    )
    assert parse_captions(vtt) == "hello world again second & last"


def test_parse_srt() -> None:
    srt = "1\n00:00:00,000 --> 00:00:02,000\nline one\n\n2\n00:00:02,000 --> 00:00:04,000\nline two\n"
    assert parse_captions(srt) == "line one line two"


def test_local_files_by_id_and_title(tmp_path: Path) -> None:
    d = tmp_path / "t"
    d.mkdir()
    (d / "vid1.txt").write_text("In 2024 we shipped\n2024\nthen 3 more\n", encoding="utf-8")
    (d / "Some Title.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nfrom srt\n", encoding="utf-8")
    (d / "empty.txt").write_text("", encoding="utf-8")
    tm = TranscriptManager(cache_dir=tmp_path / "c", transcripts_dir=d)
    assert tm._check_local_text_files("vid1") == "In 2024 we shipped 2024 then 3 more"
    assert tm._check_local_text_files("nope", "Some Title!") == "from srt"
    assert tm._check_local_text_files("empty") is None


# --- remote + cache -------------------------------------------------------------------------------


class _Snippet(SimpleNamespace):
    pass


class _Api:
    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.fetches: list[tuple[str, tuple]] = []

    def fetch(self, video_id: str, languages=("en",)):
        self.fetches.append((video_id, tuple(languages)))
        return self.behaviour(video_id)

    def list(self, video_id: str):
        raise NoTranscriptFound(video_id, ("en",), None)


def _manager(tmp_path: Path, api: _Api, **kw) -> TranscriptManager:
    return TranscriptManager(
        cache_dir=tmp_path / "cache", api_factory=lambda: api, sleep=lambda _s: None, **kw
    )


def test_fetch_caches_text(tmp_path: Path) -> None:
    api = _Api(lambda vid: [_Snippet(text="hello"), _Snippet(text="there")])
    tm = _manager(tmp_path, api)
    first = tm.resolve("abcdefghijk", "T")
    assert (first.text, first.source) == ("hello there", "youtube")
    second = tm.resolve("abcdefghijk", "T")
    assert (second.text, second.source) == ("hello there", "cache")
    assert len(api.fetches) == 1


def test_disabled_captions_are_remembered(tmp_path: Path) -> None:
    def behaviour(vid: str):
        raise TranscriptsDisabled(vid)

    api = _Api(behaviour)
    tm = _manager(tmp_path, api)
    assert tm.resolve("abcdefghijk").source == "unavailable"
    assert tm.resolve("abcdefghijk").source == "unavailable"
    assert len(api.fetches) == 1  # the second call never reached the network


def test_negative_cache_expires(tmp_path: Path) -> None:
    api = _Api(lambda vid: [_Snippet(text="now available")])
    tm = _manager(tmp_path, api)
    stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "abcdefghijk.json").write_text(
        json.dumps({"full_text": "", "unavailable": True, "reason": "x", "checked_at": stale}),
        encoding="utf-8",
    )
    assert tm.resolve("abcdefghijk").text == "now available"


def test_blocked_is_not_cached(tmp_path: Path) -> None:
    def behaviour(vid: str):
        raise RequestBlocked(vid)

    api = _Api(behaviour)
    tm = _manager(tmp_path, api)
    assert tm.resolve("abcdefghijk").source == "blocked"
    assert not (tmp_path / "cache" / "abcdefghijk.json").exists()


def test_transient_errors_retry(tmp_path: Path) -> None:
    calls = {"n": 0}

    def behaviour(vid: str):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("flaky")
        return [_Snippet(text="ok")]

    assert _manager(tmp_path, _Api(behaviour)).resolve("abcdefghijk").text == "ok"
    assert calls["n"] == 3


def test_mock_and_empty_ids_never_hit_the_network(tmp_path: Path) -> None:
    api = _Api(lambda vid: pytest.fail("network used"))
    tm = _manager(tmp_path, api)
    assert tm.resolve("mock_001").source == "skipped"
    assert tm.resolve("").source == "skipped"


def test_cache_path_is_sanitised(tmp_path: Path) -> None:
    tm = TranscriptManager(cache_dir=tmp_path / "cache")
    path = tm._cache_path("../../evil/id")
    assert path is not None and path.parent == tmp_path / "cache"


def test_fetch_many_runs_all_items(tmp_path: Path) -> None:
    api = _Api(lambda vid: [_Snippet(text=f"text {vid}")])
    tm = _manager(tmp_path, api, max_workers=3)
    seen: list[tuple[int, int]] = []
    out = tm.fetch_many([(f"video{i:06d}", "") for i in range(7)], progress=lambda d, t: seen.append((d, t)))
    assert len(out) == 7 and all(r.text.startswith("text ") for r in out.values())
    assert seen[-1] == (7, 7)


def test_other_language_is_used_when_english_is_missing(tmp_path: Path) -> None:
    class _Transcript:
        def fetch(self):
            return [_Snippet(text="hola"), _Snippet(text="mundo")]

    class _SpanishOnly(_Api):
        def fetch(self, video_id: str, languages=("en",)):
            raise NoTranscriptFound(video_id, tuple(languages), None)

        def list(self, video_id: str):
            return iter([_Transcript()])

    result = _manager(tmp_path, _SpanishOnly(lambda vid: None)).resolve("abcdefghijk")
    assert (result.text, result.source) == ("hola mundo", "youtube")
