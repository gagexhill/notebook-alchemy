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
            usage=SimpleNamespace(total_input_tokens=1000, total_output_tokens=200),
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


def test_watch_cost_and_unlisted_model_note() -> None:
    cost, note = watch_cost(600, "gemini-4-hydrogen")
    assert cost > 0 and note is None
    _pin, _pout, unlisted_note = price_for("gemini-custom-test")
    assert unlisted_note and "gemini-4-hydrogen" in unlisted_note


def test_build_watch_requires_gemini_key(tmp_path: Path) -> None:
    engine = AlchemyEngine(AlchemyConfig(cache_dir=tmp_path / "cache", api_key="", gemini_api_key=""))
    with pytest.raises(ConfigError, match="needs a Gemini API key"):
        engine.build("@chan", watch_limit=2)


def test_build_watch_transcribes_blocked_videos_caches_and_attributes_overview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    videos = [
        ChannelVideo("vid00000001", "Captioned", "u", 300, "2026-03-01"),
        ChannelVideo("vid00000002", "Blocked Short", "u", 300, "2026-03-02"),
        ChannelVideo("vid00000003", "Too Long", "u", 6000, "2026-03-03"),  # > 90m cap
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

    report = engine.build("@chan", offline=True, watch_limit=2)
    assert len(calls) == 1  # only vid00000002 watched; vid00000003 exceeded 90m
    assert report.read == {"vid00000001", "vid00000002"}
    assert report.watched == {"vid00000002"}
    assert any("longer than 90 minutes" in n for n in report.notices)
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
