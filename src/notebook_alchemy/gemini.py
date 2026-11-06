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


class _ThinkingRejected(Exception):
    """The API refused the thinking setting we sent."""


@dataclass
class GeminiResult:
    text: str
    finish_reason: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "MAX_TOKENS"


# Statuses after which another model may still answer: gone (404), throttled, or unavailable.
# A 400 (bad request) or 401/403 (key) would fail the same way on any model.
FALLBACK_STATUSES = frozenset({404, 429, 500, 502, 503, 504})
PRIMARY_RETRIES_WITH_FALLBACK = 1


class GeminiClient:
    """Minimal generateContent client. The key is sent only in the x-goog-api-key header.

    When the primary model is unavailable the client switches to `fallback_model` for the rest of
    its life, and records a notice that callers show to the user (`drain_notices`).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        fallback_model: str | None = None,
        timeout: float = 90.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None
        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._sleep = sleep
        self._failed_over: set[str] = set()
        self._notices: list[str] = []

    @property
    def active_model(self) -> str:
        """The model the next call will try first."""
        if self.model in self._failed_over and self.fallback_model:
            return self.fallback_model
        return self.model

    def drain_notices(self) -> list[str]:
        """Messages about models that were unavailable; each is returned once."""
        notices, self._notices = self._notices, []
        return notices

    @staticmethod
    def _endpoint(model: str) -> str:
        return f"{API_ROOT}/{model}:generateContent"

    @staticmethod
    def _body(
        model: str,
        prompt: str,
        *,
        system: str | None,
        json_mode: bool,
        max_output_tokens: int,
        temperature: float,
    ) -> dict:
        config: dict = {"maxOutputTokens": max_output_tokens}
        if not model.startswith("gemini-3"):
            # AI Studio offers no temperature control for Gemini 3.x models, so none is sent.
            config["temperature"] = temperature
        if json_mode:
            config["responseMimeType"] = "application/json"
        # Thinking tokens bill at the output rate and count against maxOutputTokens.
        if model.startswith("gemini-2.5"):
            config["thinkingConfig"] = {"thinkingBudget": 0}
        elif model.startswith("gemini-3"):
            config["thinkingConfig"] = {"thinkingLevel": "low"}
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
        timeout: float | None = None,
    ) -> GeminiResult:
        models = [self.active_model]
        if self.fallback_model and self.fallback_model != models[0]:
            models.append(self.fallback_model)
        for index, model in enumerate(models):
            body = self._body(
                model,
                prompt,
                system=system,
                json_mode=json_mode,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            )
            has_next = index + 1 < len(models)
            # An overloaded model can take tens of seconds per answer, so with a fallback ready the
            # primary gets one retry, not the full set.
            attempts = min(self._retries, PRIMARY_RETRIES_WITH_FALLBACK) if has_next else self._retries
            try:
                return self._call(model, body, attempts, timeout or self._timeout)
            except GeminiError as exc:
                if has_next and (exc.status is None or exc.status in FALLBACK_STATUSES):
                    self._failed_over.add(model)
                    self._notices.append(f"{model} was unavailable ({exc.detail}); used {models[index + 1]}.")
                    continue
                raise
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    def _call(self, model: str, body: dict, retries: int, timeout: float) -> GeminiResult:
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        try:
            return self._send(model, body, headers, retries, timeout)
        except _ThinkingRejected:
            # The model does not accept this thinking setting; use its default and send again.
            del body["generationConfig"]["thinkingConfig"]
            logger.info("%s rejected the thinking setting; retrying with its default.", model)
            return self._send(model, body, headers, retries, timeout)

    def _send(self, model: str, body: dict, headers: dict, retries: int, timeout: float) -> GeminiResult:
        last_error = "no response"
        last_status: int | None = None
        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=timeout, transport=self._transport) as client:
                    response = client.post(self._endpoint(model), json=body, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"network error: {type(exc).__name__}"
                last_status = None
                response = None
            else:
                if response.status_code == 200:
                    return self._parse(response, model)
                last_error = self._describe(response)
                last_status = response.status_code
                if self._thinking_rejected(response, body):
                    raise _ThinkingRejected
                if response.status_code not in RETRY_STATUSES or self._quota_exhausted(response):
                    raise GeminiError(
                        f"Gemini rejected the request ({last_error}).",
                        hint=self._hint(response.status_code),
                        status=response.status_code,
                        detail=last_error,
                    )

            if attempt < retries:
                delay = self._delay(response, attempt)
                logger.info(
                    "%s call failed (%s); retry %d/%d in %.0fs.",
                    model,
                    scrub(last_error, self._api_key),
                    attempt + 1,
                    retries,
                    delay,
                )
                self._sleep(delay)

        raise GeminiError(
            f"{model} did not succeed after {retries + 1} attempts ({last_error}).",
            hint="Check your network and quota, or run with --offline.",
            status=last_status,
            detail=last_error,
        )

    @staticmethod
    def _quota_exhausted(response: httpx.Response) -> bool:
        """A 429 for a per-day (or longer) quota: waiting seconds will not help."""
        if response.status_code != 429:
            return False
        text = response.text.lower()
        return "perday" in text or "per day" in text or "free_tier" in text

    @staticmethod
    def _thinking_rejected(response: httpx.Response, body: dict) -> bool:
        """A 400 that names the thinking field, while we sent one."""
        if response.status_code != 400 or "thinkingConfig" not in body["generationConfig"]:
            return False
        return "thinking" in response.text.lower()

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
        if self._quota_exhausted(response):
            detail = "daily request quota exhausted (free tier limit)"
        else:
            detail = " ".join(detail.split())
            detail = detail if len(detail) <= 140 else detail[:137].rstrip() + "..."
        text = f"HTTP {response.status_code}" + (f": {detail}" if detail else "")
        return scrub(text, self._api_key)

    @staticmethod
    def _hint(status: int) -> str:
        if status in (400, 404):
            return "Check ALCHEMY_GEMINI_MODEL; the model name may be wrong or retired."
        if status in (401, 403):
            return "Check GEMINI_API_KEY and that the Generative Language API is enabled for it."
        if status == 429:
            return "Enable billing for the key's project, wait for the quota to reset, or use --offline."
        return "See the message above, or run with --offline."

    def _parse(self, response: httpx.Response, model: str) -> GeminiResult:
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
            reason = (
                "the model spent its whole output budget on thinking; raise the token limit"
                if finish == "MAX_TOKENS"
                else f"finish reason: {finish}"
            )
            raise GeminiError(f"Gemini returned an empty answer ({reason}).")
        return GeminiResult(
            text=text,
            finish_reason=finish,
            prompt_tokens=int(usage.get("promptTokenCount", 0) or 0),
            output_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
            model=model,
        )
