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


PRIMARY, BACKUP = "gemini-3.8-flash", "gemini-3.5-flash-lite"


def _router(responses: dict[str, httpx.Response]):
    """MockTransport handler answering by the model named in the URL; records the order of calls."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = str(request.url).split("/models/")[1].split(":")[0]
        calls.append(model)
        return responses[model]

    return handler, calls


def _fallback_client(handler, **kw) -> GeminiClient:
    return GeminiClient(
        KEY,
        PRIMARY,
        fallback_model=BACKUP,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        **kw,
    )


def test_overloaded_primary_falls_back_and_sticks() -> None:
    handler, calls = _router(
        {
            PRIMARY: httpx.Response(503, json={"error": {"message": "high demand"}}),
            BACKUP: httpx.Response(200, json=_ok()),
        }
    )
    client = _fallback_client(handler, retries=1)
    first = client.generate("hi")
    assert first.model == BACKUP and first.text == "hello"
    assert calls == [PRIMARY, PRIMARY, BACKUP]  # two tries on the primary, then the backup
    notices = client.drain_notices()
    assert (
        len(notices) == 1 and "high demand" in notices[0] and BACKUP in notices[0] and KEY not in notices[0]
    )
    assert client.drain_notices() == []  # each notice is reported once
    client.generate("again")
    assert calls[3:] == [BACKUP]  # stays on the backup instead of re-trying the primary


def test_a_retired_model_404_falls_back() -> None:
    handler, calls = _router(
        {
            PRIMARY: httpx.Response(404, json={"error": {"message": "no longer available to new users"}}),
            BACKUP: httpx.Response(200, json=_ok()),
        }
    )
    assert _fallback_client(handler).generate("hi").model == BACKUP
    assert calls == [PRIMARY, BACKUP]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_request_and_key_errors_do_not_fall_back(status: int) -> None:
    handler, calls = _router(
        {
            PRIMARY: httpx.Response(status, json={"error": {"message": "nope"}}),
            BACKUP: httpx.Response(200, json=_ok()),
        }
    )
    with pytest.raises(GeminiError) as err:
        _fallback_client(handler).generate("hi")
    assert calls == [PRIMARY] and err.value.status == status


def test_no_fallback_when_unset_or_the_same_model() -> None:
    for fallback in (None, "", PRIMARY):
        handler, calls = _router({PRIMARY: httpx.Response(503, json={"error": {"message": "busy"}})})
        client = GeminiClient(
            KEY,
            PRIMARY,
            fallback_model=fallback,
            transport=httpx.MockTransport(handler),
            sleep=lambda _s: None,
            retries=0,
        )
        with pytest.raises(GeminiError):
            client.generate("hi")
        assert calls == [PRIMARY]


def test_both_models_down_raises_the_last_error() -> None:
    handler, _calls = _router(
        {
            PRIMARY: httpx.Response(503, json={"error": {"message": "a"}}),
            BACKUP: httpx.Response(503, json={"error": {"message": "b"}}),
        }
    )
    with pytest.raises(GeminiError) as err:
        _fallback_client(handler, retries=0).generate("hi")
    assert BACKUP in err.value.message and err.value.status == 503


def test_empty_max_tokens_answer_names_thinking_as_the_cause() -> None:
    body = {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
    with pytest.raises(GeminiError, match="thinking"):
        _client(lambda r: httpx.Response(200, json=body)).generate("hi")


def test_with_a_fallback_the_primary_gets_one_retry_not_the_full_set() -> None:
    handler, calls = _router(
        {
            PRIMARY: httpx.Response(503, json={"error": {"message": "busy"}}),
            BACKUP: httpx.Response(200, json=_ok()),
        }
    )
    assert _fallback_client(handler).generate("hi").model == BACKUP  # default retries=3
    assert calls == [PRIMARY, PRIMARY, BACKUP]


def test_the_last_model_still_gets_every_retry() -> None:
    handler, calls = _router(
        {
            PRIMARY: httpx.Response(503, json={"error": {"message": "a"}}),
            BACKUP: httpx.Response(503, json={"error": {"message": "b"}}),
        }
    )
    with pytest.raises(GeminiError):
        _fallback_client(handler).generate("hi")
    assert calls == [PRIMARY, PRIMARY] + [BACKUP] * 4


QUOTA = {
    "error": {
        "message": "You exceeded your current quota (generate_content_free_tier_requests, limit: 20). "
        "Requests per day limit exceeded on Free Tier. Please retry in 12h39m.",
    }
}


def test_a_daily_quota_fails_over_at_once_without_retrying_or_dumping_the_error() -> None:
    sleeps: list[float] = []
    handler, calls = _router(
        {PRIMARY: httpx.Response(429, json=QUOTA), BACKUP: httpx.Response(200, json=_ok())}
    )
    client = GeminiClient(
        KEY, PRIMARY, fallback_model=BACKUP, transport=httpx.MockTransport(handler), sleep=sleeps.append
    )
    assert client.generate("hi").model == BACKUP
    assert calls == [PRIMARY, BACKUP] and sleeps == []  # no waiting on a quota that resets in hours
    (notice,) = client.drain_notices()
    assert "daily request quota exhausted" in notice and "12h39m" not in notice and len(notice) < 160


def test_a_daily_quota_with_no_fallback_explains_the_next_step() -> None:
    handler, _calls = _router({PRIMARY: httpx.Response(429, json=QUOTA)})
    client = GeminiClient(KEY, PRIMARY, transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    with pytest.raises(GeminiError) as err:
        client.generate("hi")
    assert err.value.status == 429 and "billing" in (err.value.hint or "")


def test_a_rate_limit_that_clears_in_seconds_is_still_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "Resource exhausted, retry shortly"}})
        return httpx.Response(200, json=_ok())

    assert _client(handler).generate("hi").text == "hello"
    assert attempts["n"] == 2


def test_long_error_text_is_shortened_in_the_detail() -> None:
    long_message = "x" * 400
    handler, _calls = _router({PRIMARY: httpx.Response(503, json={"error": {"message": long_message}})})
    client = GeminiClient(
        KEY, PRIMARY, transport=httpx.MockTransport(handler), sleep=lambda _s: None, retries=0
    )
    with pytest.raises(GeminiError) as err:
        client.generate("hi")
    assert len(err.value.detail) < 180
