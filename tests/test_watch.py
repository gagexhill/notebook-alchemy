"""Tests for the 2026 Google GenAI Interactions API `--watch` fallback."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from notebook_alchemy.channel import (
    ChannelScanner,
    ChannelScanResult,
    ChannelVideo,
    TranscriptManager,
    TranscriptResult,
)
from notebook_alchemy.config import DEFAULT_GEMINI_MODEL, AlchemyConfig
from notebook_alchemy.engine import AlchemyEngine
from notebook_alchemy.errors import ConfigError, GeminiError
from notebook_alchemy.watch import GeminiWatcher, price_for, scrub, watch_cost

KEY = "AIzaTESTonlyfakeGeminiKey00000000"


def _fake_sdk(
    *,
    text: str = "Register in SAM.gov first.",
    status: str = "completed",
    error: Any = None,
    raise_exc: Exception | None = None,
    output_tokens: int = 200,
) -> tuple[SimpleNamespace, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(
            status=status,
            output_text=text,
            error=error,
            usage=SimpleNamespace(total_input_tokens=1000, total_output_tokens=output_tokens),
        )

    return SimpleNamespace(interactions=SimpleNamespace(create=create)), calls


def test_scrub_removes_explicit_and_google_shaped_keys() -> None:
    msg = f"fail key={KEY} and another AIzaSyABCDEF1234567890"
    cleaned = scrub(msg, KEY)
    assert KEY not in cleaned and "AIzaSyABCDEF1234567890" not in cleaned
    assert "key=***" in cleaned and "AIza***" in cleaned


def test_watch_uses_interactions_api_and_gemini_4_hydrogen() -> None:
    sdk, calls = _fake_sdk()
    watcher = GeminiWatcher(KEY, client=sdk)
    res = watcher.watch_video("vid00000001")
    assert res.text == "Register in SAM.gov first."
    assert res.model == DEFAULT_GEMINI_MODEL == "gemini-4-hydrogen"
    assert len(calls) == 1
    sent = calls[0]
    assert sent["model"] == "gemini-4-hydrogen"
    assert sent["store"] is False
    assert sent["generation_config"]["max_output_tokens"] == 32768
    assert sent["generation_config"]["thinking_level"] == "low"
    assert sent["input"][1] == {"type": "video", "uri": "https://www.youtube.com/watch?v=vid00000001"}
    model, inp, out, cost = watcher.usage() or ("", 0, 0, 0.0)
    assert model == "gemini-4-hydrogen" and inp == 1000 and out == 200 and cost > 0


@pytest.mark.parametrize(
    ("code", "expected_stop", "needle"),
    [
        (401, True, "key was not accepted"),
        (429, True, "quota or rate limit"),
        (404, True, "not available"),
        (500, False, "transcription failed"),
    ],
)
def test_watch_maps_api_errors_and_scrubs_key(code: int, expected_stop: bool, needle: str) -> None:
    from google.genai import errors as genai_errors

    err = genai_errors.APIError(code, {"error": {"message": f"bad key={KEY}"}})
    sdk, _calls = _fake_sdk(raise_exc=err)
    watcher = GeminiWatcher(KEY, client=sdk)
    with pytest.raises(GeminiError) as exc_info:
        watcher.watch_video("vid00000001")
    assert exc_info.value.stop_run is expected_stop
    assert needle in exc_info.value.message
    assert KEY not in exc_info.value.message and KEY not in exc_info.value.detail
    assert exc_info.value.hint


def test_watch_rejects_incomplete_or_empty_interaction() -> None:
    sdk, _ = _fake_sdk(text="", status="failed", error="private video")
    watcher = GeminiWatcher(KEY, client=sdk)
    with pytest.raises(GeminiError, match="could not transcribe"):
        watcher.watch_video("vid00000001")


def test_watch_rejects_output_at_token_limit_after_recording_usage() -> None:
    sdk, _calls = _fake_sdk(output_tokens=32768)
    watcher = GeminiWatcher(KEY, client=sdk)
    with pytest.raises(GeminiError, match="output limit") as exc_info:
        watcher.watch_video("vid00000001")
    assert exc_info.value.unusable
    assert watcher.usage() is not None


def test_watch_cost_and_unlisted_model_note() -> None:
    cost, note = watch_cost(600, "gemini-4-hydrogen")
    assert cost > 0 and note is None
    _pin, _pout, unlisted_note = price_for("gemini-custom-test")
    assert unlisted_note and "gemini-4-hydrogen" in unlisted_note


def test_build_watch_requires_gemini_key(tmp_path: Path) -> None:
    engine = AlchemyEngine(AlchemyConfig(cache_dir=tmp_path / "cache", api_key="", gemini_api_key=""))
    with pytest.raises(ConfigError, match="needs a Gemini API key"):
        engine.build("@chan", watch_limit=2)


def test_build_watch_requires_estimated_budget_before_scanning(tmp_path: Path) -> None:
    engine = AlchemyEngine(AlchemyConfig(cache_dir=tmp_path / "cache", gemini_api_key=KEY))
    for invalid in (0.0, float("nan"), float("inf")):
        with pytest.raises(ConfigError, match="positive estimated USD budget"):
            engine.build("@chan", watch_limit=1, watch_budget_usd=invalid)


def test_watch_budget_stops_before_a_paid_video_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = ChannelVideo("vid00000001", "Long", "u", 6000, "2026-03-03")
    monkeypatch.setattr(
        ChannelScanner, "scan", lambda self, url, limit=None, **kw: ChannelScanResult("Chan", [video])
    )
    monkeypatch.setattr(
        TranscriptManager,
        "fetch_many",
        lambda self, items, progress=None: {
            video.id: TranscriptResult(video.id, "", "blocked", "YouTube blocked")
        },
    )
    sdk, calls = _fake_sdk(text="This should not be used.")
    engine = AlchemyEngine(AlchemyConfig(cache_dir=tmp_path / "cache", gemini_api_key=KEY))
    engine.watcher = GeminiWatcher(KEY, client=sdk)
    report = engine.build("@chan", offline=True, watch_limit=1, watch_budget_usd=0.01)
    assert calls == []
    assert report.unread[0]["video_id"] == video.id
    assert any("budget would be exceeded" in notice for notice in report.notices)


def test_watch_budget_accounts_for_charged_usage_before_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    videos = [
        ChannelVideo("vid00000001", "First", "u", 600, "2026-03-03"),
        ChannelVideo("vid00000002", "Second", "u", 600, "2026-03-03"),
    ]
    monkeypatch.setattr(
        ChannelScanner, "scan", lambda self, url, limit=None, **kw: ChannelScanResult("Chan", videos)
    )
    monkeypatch.setattr(
        TranscriptManager,
        "fetch_many",
        lambda self, items, progress=None: {
            video.id: TranscriptResult(video.id, "", "blocked", "YouTube blocked") for video in videos
        },
    )
    sdk, calls = _fake_sdk(text="Words from the first video.")
    engine = AlchemyEngine(AlchemyConfig(cache_dir=tmp_path / "cache", gemini_api_key=KEY))
    watcher = GeminiWatcher(KEY, client=sdk)
    estimate, _ = watch_cost(600, watcher.model)
    monkeypatch.setattr(watcher, "usage", lambda: (watcher.model, 1, 1, estimate * 2) if calls else None)
    engine.watcher = watcher
    report = engine.build("@chan", offline=True, watch_limit=2, watch_budget_usd=estimate * 2.5)
    assert len(calls) == 1
    assert report.unread[0]["video_id"] == videos[1].id


def test_paid_unusable_watch_answer_is_not_requested_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = ChannelVideo("vid00000001", "Video", "u", 600, "2026-03-03")
    monkeypatch.setattr(
        ChannelScanner, "scan", lambda self, url, limit=None, **kw: ChannelScanResult("Chan", [video])
    )
    monkeypatch.setattr(
        TranscriptManager,
        "fetch_many",
        lambda self, items, progress=None: {
            video.id: self._read_cache(video.id)
            or TranscriptResult(video.id, "", "blocked", "YouTube blocked")
        },
    )
    sdk, calls = _fake_sdk(text="")
    engine = AlchemyEngine(AlchemyConfig(cache_dir=tmp_path / "cache", gemini_api_key=KEY))
    engine.watcher = GeminiWatcher(KEY, client=sdk)
    first = engine.build("@chan", offline=True, watch_limit=1, watch_budget_usd=1)
    assert first.unread and len(calls) == 1
    assert engine.transcripts._read_cache(video.id).detail == "GeminiUnusable"
    second = engine.build("@chan", offline=True, watch_limit=1, watch_budget_usd=1)
    assert second.unread and len(calls) == 1


def test_build_watch_transcribes_blocked_videos_caches_and_attributes_overview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    videos = [
        ChannelVideo("vid00000001", "Captioned", "u", 300, "2026-03-01"),
        ChannelVideo("vid00000002", "Blocked Short", "u", 300, "2026-03-02"),
        ChannelVideo("vid00000003", "Long Video", "u", 6000, "2026-03-03"),
    ]
    monkeypatch.setattr(
        ChannelScanner, "scan", lambda self, url, limit=None, **kw: ChannelScanResult("Chan", videos)
    )
    monkeypatch.setattr(
        TranscriptManager,
        "fetch_many",
        lambda self, items, progress=None: {
            "vid00000001": TranscriptResult("vid00000001", "From captions directly.", "youtube"),
            "vid00000002": TranscriptResult("vid00000002", "", "blocked", "YouTube blocked"),
            "vid00000003": TranscriptResult("vid00000003", "", "blocked", "YouTube blocked"),
        },
    )
    sdk, calls = _fake_sdk(text="Spoken in blocked video two.")
    cfg = AlchemyConfig(cache_dir=tmp_path / "cache", api_key="", gemini_api_key=KEY)
    engine = AlchemyEngine(cfg)
    engine.watcher = GeminiWatcher(KEY, client=sdk)

    report = engine.build("@chan", offline=True, watch_limit=2, watch_budget_usd=10)
    assert len(calls) == 2
    assert report.read == {"vid00000001", "vid00000002", "vid00000003"}
    assert report.watched == {"vid00000002", "vid00000003"}
    overview = next(b for b in report.books if b.title == "_OVERVIEW")
    assert "Transcribed from video (Gemini)" in overview.content
    assert "Blocked Short" in overview.content
    # Cached with method=MODEL_WRITTEN: without --watch it is ignored; with --watch it is reused!
    cached_default = engine.transcripts._read_cache("vid00000002")
    cached_watch = engine.transcripts._read_cache("vid00000002", include_model_written=True)
    assert cached_default is None
    assert cached_watch is not None and cached_watch.text == "Spoken in blocked video two."


def test_estimate_includes_watch_cost_when_watch_limit_set(tmp_path: Path) -> None:
    engine = AlchemyEngine(AlchemyConfig(cache_dir=tmp_path / "cache", api_key="", gemini_api_key=KEY))
    est = engine.estimate([ChannelVideo("vid00000001", "One", "u", 600)], watch_limit=1)
    assert est.watch_videos == 1 and est.watch_cost_usd > 0 and est.gemini_model == "gemini-4-hydrogen"
