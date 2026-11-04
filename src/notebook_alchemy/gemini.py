"""Gemini API client: header authentication, retry with backoff, scrubbed errors."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .errors import GeminiError

logger = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# USD per 1M tokens, (input, output). Output includes thinking tokens.
# Source: the Gemini API pricing page. Update this table when the page changes.
PRICING_USD_PER_M: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
# An unlisted model is estimated at the highest listed rate, so the estimate errs high.
FALLBACK_PRICING_MODEL = "gemini-3.8-flash"
THINKING_NOTE = "Thinking tokens bill as output and are not in the output estimate."


def price_for(model: str) -> tuple[float, float, str | None]:
    """Return (input, output, note). The note flags an unlisted price or unbudgeted thinking."""
    notes: list[str] = []
    if model in PRICING_USD_PER_M:
        low, high = PRICING_USD_PER_M[model]
    else:
        low, high = PRICING_USD_PER_M[FALLBACK_PRICING_MODEL]
        notes.append(f"No listed price for '{model}'; estimate uses {FALLBACK_PRICING_MODEL} rates.")
    if not model.startswith("gemini-2.5"):
        notes.append(THINKING_NOTE)  # only 2.5-family requests set the thinking budget to 0
    return low, high, " ".join(notes) or None


_KEY_IN_TEXT = re.compile(r"(key=)[A-Za-z0-9_\-]+")


def scrub(text: str, api_key: str) -> str:
    """Remove the API key (and any `key=...` query fragment) from text."""
    cleaned = _KEY_IN_TEXT.sub(r"\1***", text)
    if api_key:
        cleaned = cleaned.replace(api_key, "***")
    return cleaned


@dataclass
class GeminiResult:
    text: str
    finish_reason: str
    prompt_tokens: int = 0
    output_tokens: int = 0

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "MAX_TOKENS"


class GeminiClient:
    """Minimal generateContent client. The key is sent only in the x-goog-api-key header."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 120.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._sleep = sleep

    @property
    def endpoint(self) -> str:
        return f"{API_ROOT}/{self.model}:generateContent"

    def _body(
        self,
        prompt: str,
        *,
        system: str | None,
        json_mode: bool,
        max_output_tokens: int,
        temperature: float,
    ) -> dict:
        config: dict = {"temperature": temperature, "maxOutputTokens": max_output_tokens}
        if json_mode:
            config["responseMimeType"] = "application/json"
        if self.model.startswith("gemini-2.5"):
            # Thinking tokens bill at the output rate; extraction and synthesis do not need them.
            config["thinkingConfig"] = {"thinkingBudget": 0}
        body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        body["generationConfig"] = config
        return body

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        max_output_tokens: int = 8192,
        temperature: float = 0.2,
    ) -> GeminiResult:
        body = self._body(
            prompt,
            system=system,
            json_mode=json_mode,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        last_error = "no response"

        for attempt in range(self._retries + 1):
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                    response = client.post(self.endpoint, json=body, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"network error: {type(exc).__name__}"
                response = None
            else:
                if response.status_code == 200:
                    return self._parse(response)
                last_error = self._describe(response)
                if response.status_code not in RETRY_STATUSES:
                    raise GeminiError(
                        f"Gemini rejected the request ({last_error}).",
                        hint=self._hint(response.status_code),
                    )

            if attempt < self._retries:
                delay = self._delay(response, attempt)
                logger.warning(
                    "Gemini call failed (%s); retry %d/%d in %.0fs.",
                    scrub(last_error, self._api_key),
                    attempt + 1,
                    self._retries,
                    delay,
                )
                self._sleep(delay)

        raise GeminiError(
            f"Gemini did not succeed after {self._retries + 1} attempts ({last_error}).",
            hint="Check your network and quota, or run with --offline.",
        )

    @staticmethod
    def _delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                return min(float(retry_after), 60.0)
        return float(min(2**attempt, 30))

    def _describe(self, response: httpx.Response) -> str:
        detail = ""
        try:
            detail = str(response.json().get("error", {}).get("message", ""))
        except ValueError:
            detail = response.text[:200]
        text = f"HTTP {response.status_code}" + (f": {detail}" if detail else "")
        return scrub(text, self._api_key)

    @staticmethod
    def _hint(status: int) -> str:
        if status in (400, 404):
            return "Check ALCHEMY_GEMINI_MODEL; the model name may be wrong or retired."
        if status in (401, 403):
            return "Check GEMINI_API_KEY and that the Generative Language API is enabled for it."
        return "See the message above, or run with --offline."

    def _parse(self, response: httpx.Response) -> GeminiResult:
        try:
            data = response.json()
        except ValueError as exc:
            raise GeminiError("Gemini returned a response that is not JSON.") from exc

        usage = data.get("usageMetadata", {}) or {}
        candidates = data.get("candidates") or []
        if not candidates:
            block = (data.get("promptFeedback") or {}).get("blockReason", "none")
            raise GeminiError(
                f"Gemini returned no candidates (block reason: {block}).",
                hint="Rephrase the input or run with --offline.",
            )
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
        finish = str(candidate.get("finishReason", "UNKNOWN"))
        if not text.strip():
            raise GeminiError(f"Gemini returned an empty answer (finish reason: {finish}).")
        return GeminiResult(
            text=text,
            finish_reason=finish,
            prompt_tokens=int(usage.get("promptTokenCount", 0) or 0),
            output_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
        )
