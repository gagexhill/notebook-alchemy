"""Gemini API client: header authentication, retry with backoff, scrubbed errors."""

from __future__ import annotations

import datetime
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import GeminiError

logger = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# USD per 1M tokens, (input, output). Output includes thinking tokens. Each model lists its prices
# with the date they take effect, oldest first, so a scheduled change needs no code change on the
# day. Source: the Gemini API pricing page (aistudio.google.com/docs/pricing), read 2026-11-07.
PRICING_USD_PER_M: dict[str, tuple[tuple[datetime.date, float, float], ...]] = {
    "gemini-3.8-flash": (
        (datetime.date(2026, 1, 1), 0.75, 3.75),  # introductory price, through 2026-12-31
        (datetime.date(2027, 1, 1), 1.50, 7.50),
    ),
    "gemini-3.5-flash-lite": ((datetime.date(2026, 1, 1), 0.30, 2.50),),
    "gemini-3.1-flash-lite": ((datetime.date(2026, 1, 1), 0.25, 1.50),),
    "gemini-2.5-flash": ((datetime.date(2026, 1, 1), 0.30, 2.50),),
    "gemini-2.5-flash-lite": ((datetime.date(2026, 1, 1), 0.10, 0.40),),
}
# An unlisted model is estimated at the default model's rate, so the estimate errs high.
FALLBACK_PRICING_MODEL = "gemini-3.8-flash"
BATCH_PRICE_FACTOR = 0.5  # batch mode bills half the standard rate (aistudio.google.com/docs/batch-api)
BATCH_SUFFIX = " (batch)"
THINKING_NOTE = "Thinking tokens bill as output and are not in the output estimate."


def _rates_on(model: str, day: datetime.date) -> tuple[float, float]:
    """The (input, output) price in effect on `day`: the latest entry that has started."""
    entries = PRICING_USD_PER_M[model]
    current = [e for e in entries if e[0] <= day] or [entries[0]]
    _start, low, high = current[-1]
    return low, high


def price_for(model: str, day: datetime.date | None = None) -> tuple[float, float, str | None]:
    """Return (input, output, note) for `day` (today by default).

    The note flags an unlisted price or unbudgeted thinking.
    """
    day = day or datetime.datetime.now(datetime.UTC).date()
    notes: list[str] = []
    if model in PRICING_USD_PER_M:
        low, high = _rates_on(model, day)
    else:
        low, high = _rates_on(FALLBACK_PRICING_MODEL, day)
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
    output_tokens: int = 0  # the visible answer
    model: str = ""
    thinking_tokens: int = 0  # billed as output too

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
        # Tokens Google reports per model for answered requests: [input, output incl. thinking].
        self._usage: dict[str, list[int]] = {}
        self._usage_lock = threading.Lock()

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
        allow_fallback: bool = True,
    ) -> GeminiResult:
        """One answer. With `allow_fallback=False` only the configured model answers: a step whose
        results are cached per model (labelling) stops on a quota instead of mixing in another model."""
        models = [self.active_model if allow_fallback else self.model]
        if allow_fallback and self.fallback_model and self.fallback_model != models[0]:
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
                result = self._call(model, body, attempts, timeout or self._timeout)
            except GeminiError as exc:
                if has_next and (exc.status is None or exc.status in FALLBACK_STATUSES):
                    self._failed_over.add(model)
                    self._notices.append(f"{model} was unavailable ({exc.detail}); used {models[index + 1]}.")
                    continue
                raise
            self._record_usage(result)
            return result
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    def _record_usage(self, result: GeminiResult, *, batch: bool = False) -> None:
        with self._usage_lock:
            totals = self._usage.setdefault(result.model + (BATCH_SUFFIX if batch else ""), [0, 0])
            totals[0] += result.prompt_tokens
            totals[1] += result.output_tokens + result.thinking_tokens

    def usage(self) -> list[tuple[str, int, int, float]]:
        """(model, input tokens, output tokens including thinking, USD at today's listed price).

        Counts only requests Gemini answered, as Google reported them; a failed request can still be
        billed, so this is a floor.
        """
        rows = []
        with self._usage_lock:
            items = sorted(self._usage.items())
        for model, (tokens_in, tokens_out) in items:
            base = model.removesuffix(BATCH_SUFFIX)
            price_in, price_out, _note = price_for(base)
            factor = BATCH_PRICE_FACTOR if model.endswith(BATCH_SUFFIX) else 1.0
            cost = (tokens_in * price_in + tokens_out * price_out) / 1e6 * factor
            rows.append((model, tokens_in, tokens_out, cost))
        return rows

    # -- batch mode: half price, asynchronous; Google refuses it for projects without billing --

    def submit_batch(self, requests: list[tuple[str, dict]], display_name: str) -> str:
        """Submit (key, generateContent body) pairs as one inline batch; return the job name.

        Raises GeminiError; `batch_refused` tells a project that cannot use batch mode apart.
        """
        body = {
            "batch": {
                "display_name": display_name,
                "input_config": {
                    "requests": {"requests": [{"request": b, "metadata": {"key": k}} for k, b in requests]}
                },
            }
        }
        data = self._rest("POST", f"{API_ROOT}/{self.model}:batchGenerateContent", body)
        name = data.get("name") or (data.get("metadata") or {}).get("name")
        if not name:
            raise GeminiError("Gemini accepted the batch but returned no job name.")
        return str(name)

    def batch_results(
        self, name: str, keys: list[str]
    ) -> tuple[str, dict[str, GeminiResult | GeminiError] | None]:
        """(state, results by key once the job succeeded). Keys come from each response, else order."""
        data = self._rest("GET", f"{API_ROOT.rsplit('/models', 1)[0]}/{name}", None)
        meta = data.get("metadata") or {}
        state = str(data.get("state") or meta.get("state") or "")
        if not state.endswith("SUCCEEDED"):
            return state, None
        holder = data.get("dest") or data.get("response") or meta.get("output") or {}
        inlined = holder.get("inlinedResponses") or holder.get("inlined_responses") or []
        if isinstance(inlined, dict):  # nested one level deeper in some responses
            inlined = inlined.get("inlinedResponses") or []
        results: dict[str, GeminiResult | GeminiError] = {}
        for index, item in enumerate(inlined):
            key = str(
                (item.get("metadata") or {}).get("key") or (keys[index] if index < len(keys) else index)
            )
            if item.get("error"):
                message = str((item["error"] or {}).get("message", "request failed"))
                results[key] = GeminiError(scrub(f"Gemini batch request failed: {message}", self._api_key))
                continue
            try:
                result = self.parse_payload(item.get("response") or {}, self.model)
            except GeminiError as exc:
                results[key] = exc
                continue
            self._record_usage(result, batch=True)
            results[key] = result
        return state, results

    @staticmethod
    def batch_refused(exc: GeminiError) -> bool:
        """The project cannot use batch mode (Google answers FAILED_PRECONDITION without billing)."""
        return exc.status == 400 and "precondition" in exc.detail.lower()

    def _rest(self, method: str, url: str, body: dict | None) -> dict:
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.request(method, url, json=body, headers=headers)
        except httpx.TransportError as exc:
            raise GeminiError(f"Gemini batch request failed: network error: {type(exc).__name__}") from exc
        if response.status_code != 200:
            detail = self._describe(response)
            raise GeminiError(
                f"Gemini refused the batch request ({detail}).",
                hint=self._hint(response.status_code),
                status=response.status_code,
                detail=detail,
                quota=self._quota_exhausted(response),
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GeminiError("Gemini returned a batch response that is not JSON.") from exc

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
                        quota=self._quota_exhausted(response),
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
    def _error_details(response: httpx.Response, kind: str) -> list[dict[str, Any]]:
        """Google's structured error details of one type (`QuotaFailure`, `RetryInfo`, ...)."""
        try:
            details = response.json().get("error", {}).get("details", [])
        except (ValueError, AttributeError):
            return []
        if not isinstance(details, list):
            return []
        return [d for d in details if isinstance(d, dict) and str(d.get("@type", "")).endswith(f".{kind}")]

    @classmethod
    def _quota_exhausted(cls, response: httpx.Response) -> bool:
        """A 429 for a per-day (or longer) quota: waiting seconds will not help.

        Read from the structured QuotaFailure detail when Google sends one; the message text is
        only a fallback, because its wording can change.
        """
        if response.status_code != 429:
            return False
        failures = cls._error_details(response, "QuotaFailure")
        if failures:
            quota_ids = [
                str(v.get("quotaId", "")) + str(v.get("quotaMetric", ""))
                for f in failures
                for v in f.get("violations", [])
                if isinstance(v, dict)
            ]
            return any("perday" in q.lower() for q in quota_ids)
        text = response.text.lower()
        return "perday" in text or "per day" in text or "free_tier" in text

    @staticmethod
    def _thinking_rejected(response: httpx.Response, body: dict) -> bool:
        """A 400 that names the thinking field, while we sent one."""
        if response.status_code != 400 or "thinkingConfig" not in body["generationConfig"]:
            return False
        return "thinking" in response.text.lower()

    @classmethod
    def _delay(cls, response: httpx.Response | None, attempt: int) -> float:
        """Google's RetryInfo delay, else the Retry-After header, else exponential backoff."""
        if response is not None:
            for info in cls._error_details(response, "RetryInfo"):
                match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(info.get("retryDelay", "")))
                if match:
                    return min(float(match.group(1)), 60.0)
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
        return self.parse_payload(data, model)

    @staticmethod
    def parse_payload(data: dict, model: str) -> GeminiResult:
        """A GeminiResult from a generateContent response body (direct or from a batch)."""
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
            thinking_tokens=int(usage.get("thoughtsTokenCount", 0) or 0),
        )
