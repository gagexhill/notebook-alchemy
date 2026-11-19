"""Opt-in `--watch` fallback: transcribe blocked YouTube videos via 2026 Google GenAI Interactions API.

Used only when YouTube captions, yt-dlp subtitles, and NotebookLM all fail to yield words for a
public YouTube video. Uses `google-genai >= 3.2.0` (`client.interactions.create`) with
`gemini-4-hydrogen` by default, `store=False`, and scrubbed errors.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from .config import DEFAULT_GEMINI_MODEL, GEMINI_KEY_URL
from .errors import GeminiError

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output). Source: Google AI Studio pricing, read 2026-11-19.
PRICING_USD_PER_M: dict[str, tuple[float, float]] = {
    "gemini-4-hydrogen": (1.50, 7.50),
    "gemini-4-argon": (1.50, 7.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}
FALLBACK_PRICING_MODEL = DEFAULT_GEMINI_MODEL
WATCH_TOKENS_PER_SECOND = 100
WATCH_MAX_OUTPUT_TOKENS = 32768
MAX_WATCH_DURATION_SECONDS = 5400  # 90 minutes per video cap
DEFAULT_WATCH_TIMEOUT = 600.0

WATCH_SYSTEM = (
    "You transcribe spoken words from public YouTube videos accurately and verbatim. Do not summarize, "
    "paraphrase, or invent words. Return only the spoken transcript as plain text."
)
WATCH_PROMPT = (
    "Write down what the speakers say in this video, word for word, with normal punctuation and "
    "paragraph breaks. Ignore background music and visual-only captions unless spoken. Return only "
    "the transcript text."
)

_AIZA_RE = re.compile(r"AIza[A-Za-z0-9_\-]+")
_KEY_PARAM_RE = re.compile(r"(key=)[A-Za-z0-9_\-]+")


def scrub(text: str, api_key: str = "") -> str:
    """Remove GEMINI_API_KEY and Google API key shapes from text."""
    cleaned = text.replace(api_key, "***") if api_key else text
    cleaned = _KEY_PARAM_RE.sub(r"\1***", cleaned)
    return _AIZA_RE.sub("AIza***", cleaned)


def price_for(model: str) -> tuple[float, float, str | None]:
    """(input, output, note) USD per 1M tokens for `model`."""
    rates = PRICING_USD_PER_M.get(model)
    if rates is not None:
        return rates[0], rates[1], None
    fallback = PRICING_USD_PER_M[FALLBACK_PRICING_MODEL]
    note = f"No listed price for '{model}'; watch costs are shown at {FALLBACK_PRICING_MODEL} rates."
    return fallback[0], fallback[1], note


def watch_cost(seconds: int, model: str) -> tuple[float, str | None]:
    """Estimated USD cost to watch a video of `seconds` duration via Interactions API."""
    if seconds <= 0:
        return 0.0, None
    price_in, price_out, note = price_for(model)
    words_out = seconds * 2.5
    cost = (seconds * WATCH_TOKENS_PER_SECOND * price_in + words_out * 1.35 * price_out) / 1e6
    return cost, note


@dataclass
class WatchResult:
    video_id: str
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cost_usd: float = 0.0


class GeminiWatcher:
    """Reads public YouTube videos via Google GenAI Interactions API (`client.interactions.create`)."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        *,
        timeout: float = DEFAULT_WATCH_TIMEOUT,
        client: Any = None,
    ) -> None:
        self.model = model or DEFAULT_GEMINI_MODEL
        self._api_key = api_key
        self._timeout = timeout
        self._sdk = client
        self._lock = threading.Lock()
        self._usage_tokens = [0, 0]
        self._usage_cost = 0.0

    @property
    def _client(self) -> Any:
        with self._lock:
            if self._sdk is None:
                from google import genai

                self._sdk = genai.Client(api_key=self._api_key)
            return self._sdk

    def watch_video(self, video_id: str) -> WatchResult:
        """Transcribe one public YouTube video via the Interactions API. Raises GeminiError."""
        from google.genai import errors as genai_errors

        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            interaction = self._client.interactions.create(
                model=self.model,
                input=[
                    {"type": "text", "text": WATCH_PROMPT},
                    {"type": "video", "uri": url},
                ],
                system_instruction=WATCH_SYSTEM,
                generation_config={
                    "max_output_tokens": WATCH_MAX_OUTPUT_TOKENS,
                    "thinking_level": "low",
                },
                store=False,
                timeout=self._timeout,
            )
        except genai_errors.APIError as exc:
            raise self._api_error(exc) from None
        except Exception as exc:  # noqa: BLE001 - network/transport errors vary; scrubbed before raising
            raise GeminiError(
                f"Could not reach the Gemini API while watching {video_id}.",
                hint="Check your internet connection and run the same command again.",
                detail=scrub(f"{type(exc).__name__}: {exc}", self._api_key)[:300],
            ) from None

        status = str(getattr(interaction, "status", "") or "").lower()
        usage = getattr(interaction, "usage", None)
        in_tok = int(getattr(usage, "total_input_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "total_output_tokens", 0) or getattr(usage, "output_tokens", 0) or 0)
        price_in, price_out, _ = price_for(self.model)
        cost = (in_tok * price_in + out_tok * price_out) / 1e6
        with self._lock:
            self._usage_tokens[0] += in_tok
            self._usage_tokens[1] += out_tok
            self._usage_cost += cost

        text = " ".join(str(getattr(interaction, "output_text", "") or "").split())
        if status not in ("", "completed") or not text:
            err_detail = scrub(str(getattr(interaction, "error", "") or status or "empty"), self._api_key)
            raise GeminiError(
                f"Gemini could not transcribe {video_id} ({err_detail}).",
                hint="The video may be private, unlisted, or have no speech; it waits for the next run.",
                detail=err_detail[:200],
            )
        return WatchResult(
            video_id=video_id,
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=self.model,
            cost_usd=cost,
        )

    def _api_error(self, exc: Any) -> GeminiError:
        code = int(getattr(exc, "code", 0) or 0)
        raw_msg = getattr(exc, "message", "") or str(exc)
        detail = scrub(f"HTTP {code}: {raw_msg}", self._api_key)[:300]
        lowered = detail.lower()
        if code in (401, 403) or "api key" in lowered or "permission" in lowered:
            return GeminiError(
                "The Gemini API key was not accepted.",
                hint=f"Check GEMINI_API_KEY in your .env (create one at {GEMINI_KEY_URL}).",
                status=code,
                detail=detail,
                stop_run=True,
            )
        if code == 429 or "quota" in lowered or "resource_exhausted" in lowered:
            return GeminiError(
                "The Gemini API quota or rate limit was reached while watching videos.",
                hint="Wait for quota reset or check billing in Google AI Studio; cached words are kept.",
                status=code,
                detail=detail,
                stop_run=True,
            )
        if code == 404:
            return GeminiError(
                f"The Gemini model '{self.model}' is not available to this key.",
                hint="Unset ALCHEMY_GEMINI_MODEL to use the default model.",
                status=code,
                detail=detail,
                stop_run=True,
            )
        return GeminiError(
            f"Gemini video transcription failed ({detail}).",
            hint="Run the same command again to retry unread videos.",
            status=code,
            detail=detail,
        )

    def usage(self) -> tuple[str, int, int, float] | None:
        with self._lock:
            if not (self._usage_tokens[0] or self._usage_tokens[1] or self._usage_cost):
                return None
            return (self.model, self._usage_tokens[0], self._usage_tokens[1], self._usage_cost)
