"""Channel scanning and transcript resolution."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from youtube_transcript_api import (
    AgeRestricted,
    FailedToCreateConsentCookie,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
)

from notebook_alchemy.channel import (
    ChannelScanner,
    SubtitlesBlocked,
    SubtitlesSetupError,
    SubtitlesUnavailable,
    TranscriptManager,
    YtDlpSubtitles,
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
        ("Portable Entrepreneur", False),
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
    monkeypatch.setattr("yt_dlp.YoutubeDL", _FakeYDL)
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


def test_a_channel_reads_videos_and_streams_but_not_shorts_by_default(ydl: type[_FakeYDL]) -> None:
    ydl.script["https://www.youtube.com/@x/videos"] = {
        "title": "X - Videos",
        "entries": [_entry(1), _entry(2)],
    }
    ydl.script["https://www.youtube.com/@x/streams"] = {
        "title": "X - Live",
        "entries": [_entry(3), _entry(1)],
    }
    ydl.script["https://www.youtube.com/@x/shorts"] = {"title": "X - Shorts", "entries": [_entry(9)]}
    result = ChannelScanner().scan("@x")
    assert result.channel_name == "X"
    assert [v.id for v in result.videos] == ["vid00000001", "vid00000002", "vid00000003"]  # deduplicated
    everything = ChannelScanner().scan("https://www.youtube.com/@x", tabs=("videos", "streams", "shorts"))
    assert "vid00000009" in [v.id for v in everything.videos]


def test_a_tab_the_channel_lacks_is_skipped_and_the_limit_counts_all_tabs(ydl: type[_FakeYDL]) -> None:
    ydl.script["https://www.youtube.com/@x/videos"] = {
        "title": "X - Videos",
        "entries": [_entry(1), _entry(2)],
    }
    ydl.script["https://www.youtube.com/@x/streams"] = RuntimeError(
        "This channel does not have a streams tab"
    )
    assert [v.id for v in ChannelScanner().scan("@x", limit=1).videos] == ["vid00000001"]
    assert len(ChannelScanner().scan("@x").videos) == 2


def test_a_playlist_ignores_tabs(ydl: type[_FakeYDL]) -> None:
    url = "https://www.youtube.com/playlist?list=PL123"
    ydl.script[url] = {"title": "List", "entries": [_entry(4)]}
    assert [v.id for v in ChannelScanner().scan(url, tabs=("shorts",)).videos] == ["vid00000004"]


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
    stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
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


def test_a_second_proof_of_origin_demand_stops_later_requests(tmp_path: Path) -> None:
    def behaviour(vid: str):
        raise PoTokenRequired(vid)

    api = _Api(behaviour)
    tm = _manager(tmp_path, api)
    results = [tm.resolve(v) for v in ("abcdefghijk", "bcdefghijkl", "cdefghijklm")]
    assert [r.source for r in results] == ["blocked", "blocked", "blocked"]
    assert len(api.fetches) == 2  # no retries; the third video never asked
    assert not (tmp_path / "cache").exists()  # a block is not remembered as "no captions"


def test_a_consent_page_failure_is_a_block_not_missing_captions(tmp_path: Path) -> None:
    def behaviour(vid: str):
        raise FailedToCreateConsentCookie(vid)

    api = _Api(behaviour)
    tm = _manager(tmp_path, api)
    assert tm.resolve("abcdefghijk").source == "blocked"
    assert tm.resolve("bcdefghijkl").source == "blocked" and len(api.fetches) == 1
    assert not (tmp_path / "cache").exists()


def test_only_youtube_video_ids_reach_the_captions_api(tmp_path: Path) -> None:
    api = _Api(lambda vid: pytest.fail("network used"))
    tm = _manager(tmp_path, api)
    for vid in ("mock_001", "", "0f3c2a9e-uuid-from-notebooklm", "short"):
        assert tm.resolve(vid).source == "skipped"


def test_any_other_retrieval_failure_is_remembered_as_unavailable(tmp_path: Path) -> None:
    def behaviour(vid: str):
        raise AgeRestricted(vid)

    api = _Api(behaviour)
    tm = _manager(tmp_path, api)
    assert tm.resolve("abcdefghijk").source == "unavailable"
    assert tm.resolve("abcdefghijk").detail == "AgeRestricted" and len(api.fetches) == 1


class _Track(SimpleNamespace):
    def fetch(self):
        return [_Snippet(text=self.text)]


def test_a_written_caption_track_is_preferred_over_an_automatic_one(tmp_path: Path) -> None:
    class Api(_Api):
        def list(self, video_id: str):
            return [_Track(is_generated=True, text="auto"), _Track(is_generated=False, text="written")]

    def behaviour(vid: str):
        raise NoTranscriptFound(vid, ("en",), None)

    assert _manager(tmp_path, Api(behaviour)).resolve("abcdefghijk").text == "written"


def test_caption_languages_are_asked_for_in_order(tmp_path: Path) -> None:
    api = _Api(lambda vid: [_Snippet(text="hola")])
    _manager(tmp_path, api, languages=("es", "en")).resolve("abcdefghijk")
    assert api.fetches == [("abcdefghijk", ("es", "en"))]


def test_transient_errors_retry(tmp_path: Path) -> None:
    calls = {"n": 0}

    def behaviour(vid: str):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("flaky")
        return [_Snippet(text="ok")]

    assert _manager(tmp_path, _Api(behaviour)).resolve("abcdefghijk").text == "ok"
    assert calls["n"] == 3


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


# --- dates, routes and pacing ------------------------------------------------------------------------


def test_a_channel_listing_gives_each_video_a_date(ydl: type[_FakeYDL]) -> None:
    stamped = {**_entry(1), "timestamp": 1_700_000_000}  # 2023-11-14
    exact = {**_entry(2), "upload_date": "20240105"}
    ydl.script["https://www.youtube.com/@x/videos"] = {
        "title": "X - Videos",
        "entries": [stamped, exact, _entry(3)],
    }
    videos = ChannelScanner().scan("@x", tabs=("videos",)).videos
    assert [v.published for v in videos] == ["2023-11-14", "2024-01-05", ""]
    assert ydl.opts_seen[0]["extractor_args"] == {"youtubetab": {"approximate_date": [""]}}


class _Subs:
    """A scripted yt-dlp route: text, or an exception, per video."""

    def __init__(self, script=None) -> None:
        self.script = script or {}
        self.calls: list[str] = []

    def fetch(self, video_id: str):
        self.calls.append(video_id)
        out = self.script.get(video_id, ("subtitle words", "2024-03-02"))
        if isinstance(out, Exception):
            raise out
        return out


def _blocked_api(vid: str):
    raise RequestBlocked(vid)


def test_a_block_moves_on_to_yt_dlp_instead_of_stopping_the_run(tmp_path: Path) -> None:
    api, subs = _Api(_blocked_api), _Subs()
    tm = _manager(tmp_path, api, subtitles=subs)
    results = tm.fetch_many([(f"vid{i:08d}", "") for i in range(3)])
    assert [r.source for r in results.values()] == ["subtitles"] * 3
    assert len(api.fetches) == 1  # the captions route paused after its block; the others went on
    cached = json.loads((tmp_path / "cache" / "vid00000002.json").read_text(encoding="utf-8"))
    assert cached["method"] == "subtitles" and cached["published"] == "2024-03-02"
    assert tm.resolve("vid00000002").published == "2024-03-02"  # read back from the cache


def test_no_captions_on_youtube_is_final_and_skips_yt_dlp(tmp_path: Path) -> None:
    def disabled(vid: str):
        raise TranscriptsDisabled(vid)

    subs = _Subs()
    assert _manager(tmp_path, _Api(disabled), subtitles=subs).resolve("abcdefghijk").source == "unavailable"
    assert subs.calls == []


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_blocked_videos_are_tried_again_when_youtube_reopens(tmp_path: Path) -> None:
    clock = _Clock()
    state = {"blocked": True}

    def api_behaviour(vid: str):
        if state["blocked"]:
            raise RequestBlocked(vid)
        return [_Snippet(text=f"words {vid}")]

    subs = _Subs({f"vid{i:08d}": SubtitlesBlocked("not a bot") for i in range(2)})
    tm = TranscriptManager(
        cache_dir=tmp_path / "cache",
        api_factory=lambda: _Api(api_behaviour),
        subtitles=subs,
        sleep=lambda s: (clock.sleep(s), state.update(blocked=False)),
        clock=clock,
        block_pause=60,
    )
    results = tm.fetch_many([("vid00000000", ""), ("vid00000001", "")])
    assert [r.source for r in results.values()] == ["youtube", "youtube"]
    assert 60 in clock.sleeps and tm.blocked_total == 0


def test_a_block_is_never_cached_and_is_reported(tmp_path: Path) -> None:
    subs = _Subs({"abcdefghijk": SubtitlesBlocked("HTTP Error 429")})
    tm = _manager(tmp_path, _Api(_blocked_api), subtitles=subs, block_pause=0)
    result = tm.fetch_many([("abcdefghijk", "")])["abcdefghijk"]
    assert result.source == "blocked" and tm.blocked_total == 1
    assert not (tmp_path / "cache" / "abcdefghijk.json").exists()


def test_remote_requests_are_spaced(tmp_path: Path) -> None:
    clock = _Clock()
    tm = TranscriptManager(
        cache_dir=tmp_path / "cache",
        api_factory=lambda: _Api(lambda vid: [_Snippet(text="hi")]),
        sleep=clock.sleep,
        clock=clock,
        spacing=(3.0, 8.0),
        rand=lambda low, high: 5.0,
        max_workers=1,
    )
    tm.fetch_many([(f"vid{i:08d}", "") for i in range(3)])
    assert clock.sleeps == [5.0, 5.0]  # none before the first request, one gap before each other
    clock.sleeps.clear()
    tm.fetch_many([(f"vid{i:08d}", "") for i in range(3)])  # all cached now: no waiting
    assert clock.sleeps == []


_VTT = "WEBVTT\n\n00:00.000 --> 00:01.000\nhi there\n"


class _SubsYDL:
    """Stands in for yt_dlp.YoutubeDL inside YtDlpSubtitles."""

    def __init__(self, info=None, error: str | None = None, body: str = _VTT) -> None:
        self.info, self.error, self.body = info or {}, error, body
        self.opts: dict = {}
        self.opened: list[str] = []

    def __call__(self, opts):
        self.opts = opts
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def extract_info(self, url, download=False):
        if self.error:
            self.opts["logger"].error(self.error)
            raise RuntimeError("DownloadError")
        return self.info

    def urlopen(self, url):
        self.opened.append(url)
        return SimpleNamespace(read=lambda: self.body.encode("utf-8"))


def test_yt_dlp_prefers_a_written_track_in_a_preferred_language_and_reads_the_date() -> None:
    info = {
        "upload_date": "20250911",
        "subtitles": {"en": [{"ext": "json3", "url": "j"}, {"ext": "vtt", "url": "written-en"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "auto-en"}]},
    }
    fake = _SubsYDL(info)
    text, published = YtDlpSubtitles(ydl_factory=fake, cookies_browser="chrome").fetch("abcdefghijk")
    assert (text, published) == ("hi there", "2025-09-11")
    assert fake.opened == ["written-en"] and fake.opts["cookiesfrombrowser"] == ("chrome",)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ("Sign in to confirm you're not a bot", SubtitlesBlocked),
        ("HTTP Error 429: Too Many Requests", SubtitlesBlocked),
        ("Private video", SubtitlesUnavailable),
    ],
)
def test_yt_dlp_failures_are_told_apart(error: str, kind: type[Exception]) -> None:
    with pytest.raises(kind):
        YtDlpSubtitles(ydl_factory=_SubsYDL(error=error)).fetch("abcdefghijk")


def test_yt_dlp_with_no_track_is_unavailable() -> None:
    with pytest.raises(SubtitlesUnavailable):
        YtDlpSubtitles(ydl_factory=_SubsYDL({"subtitles": {}, "automatic_captions": {}})).fetch("abcdefghijk")


def test_videos_youtube_refuses_are_read_through_notebooklm_and_cached(tmp_path: Path) -> None:
    def disabled_or_blocked(vid: str):
        if vid == "nocaptions1":
            raise TranscriptsDisabled(vid)
        raise RequestBlocked(vid)

    asked: list[list[str]] = []

    def bulk(items):
        asked.append([vid for vid, _ in items])
        return {vid: f"notebooklm words {vid}" for vid, _ in items}

    tm = _manager(tmp_path, _Api(disabled_or_blocked), bulk=bulk, block_pause=0)
    results = tm.fetch_many([("vid00000001", "a"), ("nocaptions1", "b"), ("vid00000002", "c")])
    assert asked == [["vid00000001", "vid00000002"]]  # a video with no captions at all is not sent
    assert results["vid00000001"].source == "notebooklm" and tm.blocked_total == 0
    cached = json.loads((tmp_path / "cache" / "vid00000002.json").read_text(encoding="utf-8"))
    assert cached["method"] == "notebooklm" and cached["full_text"] == "notebooklm words vid00000002"


def test_a_browser_sign_in_yt_dlp_cannot_read_closes_the_route_once_with_a_notice(tmp_path: Path) -> None:
    class Unreadable(_Subs):
        def fetch(self, video_id: str):
            self.calls.append(video_id)
            raise SubtitlesSetupError("yt-dlp could not read the chrome sign-in")

    subs = Unreadable()
    tm = _manager(tmp_path, _Api(_blocked_api), subtitles=subs, block_pause=0)
    tm.fetch_many([(f"vid{i:08d}", "") for i in range(3)])
    assert subs.calls == ["vid00000000"] and tm.notices == ["yt-dlp could not read the chrome sign-in"]


def test_a_cookie_failure_is_a_setup_error_and_a_path_with_age_in_it_is_not_a_missing_video() -> None:
    error = "could not find firefox cookies database in 'c:\\appdata\\local\\packages\\x'"
    with pytest.raises(SubtitlesSetupError):
        YtDlpSubtitles(ydl_factory=_SubsYDL(error=error), cookies_browser="firefox").fetch("abcdefghijk")
