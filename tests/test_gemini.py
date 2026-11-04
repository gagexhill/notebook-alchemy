"""Gemini client: key handling, retries, parsing."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from notebook_alchemy.errors import GeminiError
from notebook_alchemy.gemini import THINKING_NOTE, GeminiClient, price_for, scrub

KEY = "AIzaSyFAKEKEYFORTESTS0123456789"


def _ok(text: str = "hello", finish: str = "STOP") -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }


def _client(handler, model: str = "gemini-2.5-flash-lite", **kw) -> GeminiClient:
    return GeminiClient(KEY, model, transport=httpx.MockTransport(handler), sleep=lambda _s: None, **kw)


def test_key_travels_only_in_the_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok())

    result = _client(handler).generate("hi")
    assert result.text == "hello"
    request = seen[0]
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    assert "key=" not in str(request.url)


def test_retries_transient_status_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=_ok())

    assert _client(handler).generate("hi").text == "hello"
    assert attempts["n"] == 3


def test_permanent_error_is_not_retried_and_hides_the_key(caplog: pytest.LogCaptureFixture) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, json={"error": {"message": f"model not found key={KEY}"}})

    with caplog.at_level(logging.DEBUG), pytest.raises(GeminiError) as err:
        _client(handler).generate("hi")
    assert attempts["n"] == 1
    assert KEY not in err.value.message
    assert KEY not in caplog.text
    assert "ALCHEMY_GEMINI_MODEL" in (err.value.hint or "")


def test_exhausted_retries_raise_without_the_key(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=f"unavailable for url ?key={KEY}")

    with caplog.at_level(logging.DEBUG), pytest.raises(GeminiError) as err:
        _client(handler, retries=2).generate("hi")
    assert "3 attempts" in err.value.message
    assert KEY not in err.value.message
    assert KEY not in caplog.text


def test_truncation_is_reported() -> None:
    result = _client(lambda r: httpx.Response(200, json=_ok(finish="MAX_TOKENS"))).generate("hi")
    assert result.truncated


def test_no_candidates_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(GeminiError) as err:
        _client(handler).generate("hi")
    assert "SAFETY" in err.value.message


def test_thinking_and_temperature_follow_the_model_family() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_ok())

    _client(handler, model="gemini-2.5-flash").generate("hi", json_mode=True)
    _client(handler, model="gemini-3.8-flash").generate("hi")
    _client(handler, model="gemini-test-model").generate("hi")
    old, new, other = (b["generationConfig"] for b in bodies)
    assert old["thinkingConfig"] == {"thinkingBudget": 0} and old["temperature"] == 0.2
    assert old["responseMimeType"] == "application/json"
    assert new["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "temperature" not in new  # AI Studio offers no temperature control for Gemini 3.x
    assert "thinkingConfig" not in other and other["temperature"] == 0.2


def test_a_rejected_thinking_setting_is_dropped_and_retried_once() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if "thinkingConfig" in bodies[-1]["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Unknown field thinkingLevel"}})
        return httpx.Response(200, json=_ok())

    result = _client(handler, model="gemini-3.8-flash", retries=0).generate("hi")
    assert result.text == "hello" and len(bodies) == 2
    assert "thinkingConfig" not in bodies[1]["generationConfig"]


def test_scrub_removes_key_forms() -> None:
    assert KEY not in scrub(f"x https://h/p?key={KEY} y {KEY}", KEY)


def test_price_table() -> None:
    assert price_for("gemini-2.5-flash-lite") == (0.10, 0.40, None)
    low, high, note = price_for("gemini-3.8-flash")
    assert (low, high) == (0.75, 3.75)
    assert note == THINKING_NOTE  # 3.x models think by default, so the output estimate is a floor


def test_unlisted_model_is_priced_high_and_flagged() -> None:
    low, high, note = price_for("gemini-unknown")
    assert (low, high) == (0.75, 3.75)
    assert note and "gemini-unknown" in note and THINKING_NOTE in note
