"""Claude API client: the official SDK, structured JSON answers, half-price batches, scrubbed errors.

Every request asks for JSON that matches a schema (structured outputs), with thinking off and low
effort: measured on this tool's labelling task, that agreed best with careful labels and cost least.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

import anthropic

from .config import DEFAULT_MODEL
from .errors import ClaudeError

logger = logging.getLogger(__name__)

# USD per 1M tokens: (input, output) up to LONG_PROMPT_TOKENS of prompt, and above it. Source: Claude
# API pricing (platform.claude.com/docs/en/about-claude/pricing), read 2026-11-13.
PRICING_USD_PER_M: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "claude-haiku-5-5": ((0.10, 0.50), (0.50, 2.50)),
}
LONG_PROMPT_TOKENS = 100_000
# An unlisted model is priced at the default model's rate and flagged with a note, which `dry-run` and
# the cost line show: a larger model costs more, so the figure is then too low.
FALLBACK_PRICING_MODEL = "claude-haiku-5-5"
BATCH_PRICE_FACTOR = 0.5  # the Message Batches API bills half the standard rate
BATCH_SUFFIX = " (batch)"
DEFAULT_TIMEOUT = 300.0
# Fixed, never read from the environment: the SDK would otherwise take ANTHROPIC_BASE_URL, which a
# `.env` in the folder a run starts from can set, and send the key there.
API_BASE_URL = "https://api.anthropic.com"
DEFAULT_MAX_RETRIES = 4  # the SDK backs off on connection errors, 408, 409, 429, 5xx and 529
# A batch request's custom_id allows only these characters, so requests are numbered and the
# caller's keys are kept beside the batch id.
_CUSTOM_ID = re.compile(r"^r(\d+)$")
# Error codes (`error.details.error_code`, stable) that mean every later request fails too.
SPEND_LIMIT_CODES = frozenset({"enforced_spend_limit_reached"})
ACCOUNT_HOLD_CODES = frozenset({"organization_on_hold"})
# A used-up credit balance arrives as a 400 with no error code, so its wording is checked as well.
_LIMIT_WORDS = ("credit balance", "usage limit", "spend limit", "spending limit")


def price_for(model: str, prompt_tokens: int = 0) -> tuple[float, float, str | None]:
    """(input, output, note) USD per 1M tokens for a request with `prompt_tokens` of prompt."""
    note = None
    rates = PRICING_USD_PER_M.get(model)
    if rates is None:
        rates = PRICING_USD_PER_M[FALLBACK_PRICING_MODEL]
        note = (
            f"No listed price for '{model}'; costs are shown at {FALLBACK_PRICING_MODEL} rates and may be "
            "too low."
        )
    short, long = rates
    price_in, price_out = long if prompt_tokens > LONG_PROMPT_TOKENS else short
    return price_in, price_out, note


_KEY_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def scrub(text: str, api_key: str = "") -> str:
    """Remove the API key, and anything shaped like an Anthropic key, from text."""
    cleaned = text.replace(api_key, "***") if api_key else text
    return _KEY_SHAPE.sub("sk-ant-***", cleaned)


@dataclass
class ClaudeResult:
    text: str
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


def request_params(
    model: str, prompt: str, schema: dict[str, Any], *, system: str | None, max_tokens: int
) -> dict[str, Any]:
    """The Messages API parameters for one JSON request, shared by direct calls and batches so the
    two cannot drift apart."""
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
    }
    if system:
        params["system"] = system
    return params


def _text_of(message: Any) -> str:
    return "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")


class ClaudeClient:
    """A small wrapper over `anthropic.Anthropic`. The SDK sends the key in a request header."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: Any = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._sdk = client
        self._sdk_options = {"timeout": timeout, "max_retries": max_retries}
        self._sdk_lock = threading.Lock()
        self._usage: dict[str, list[float]] = {}
        self._usage_lock = threading.Lock()

    @property
    def _client(self) -> Any:
        """The SDK client, made on the first request: `status` and `dry-run` never need one, and making
        it reads platform details (on Windows by starting a process)."""
        with self._sdk_lock:
            if self._sdk is None:
                self._sdk = anthropic.Anthropic(
                    api_key=self._api_key, base_url=API_BASE_URL, **self._sdk_options
                )
            return self._sdk

    # -- direct calls --

    def generate(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        system: str | None = None,
        max_tokens: int = 16384,
        timeout: float | None = None,
    ) -> ClaudeResult:
        """One request; its answer is JSON matching `schema`. Raises ClaudeError."""
        params = request_params(self.model, prompt, schema, system=system, max_tokens=max_tokens)
        if timeout is not None:
            params["timeout"] = timeout
        try:
            message = self._client.messages.create(**params)
        except anthropic.APIStatusError as exc:
            raise self._status_error(exc) from None
        except anthropic.APIConnectionError as exc:
            raise ClaudeError(
                "Could not reach the Claude API.",
                hint="Check the internet connection and run the same command again.",
                detail=scrub(f"connection: {exc}", self._api_key),
            ) from None
        return self._result(message, batch=False)

    def _result(self, message: Any, *, batch: bool) -> ClaudeResult:
        usage = getattr(message, "usage", None)
        result = ClaudeResult(
            text=_text_of(message),
            stop_reason=str(getattr(message, "stop_reason", "") or ""),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=str(getattr(message, "model", "") or self.model),
        )
        self._record_usage(result, batch=batch)  # billed whether or not the answer is usable
        if result.stop_reason == "end_turn" and result.text:
            return result
        reason = {
            "refusal": "Claude declined to answer this request",
            "max_tokens": "the answer was longer than the output limit",
        }.get(result.stop_reason, f"the answer ended as '{result.stop_reason or 'empty'}'")
        raise ClaudeError(f"Unusable answer: {reason}.", detail=reason, unusable=True)

    def _status_error(self, exc: anthropic.APIStatusError) -> ClaudeError:
        status = int(getattr(exc, "status_code", 0) or 0)
        kind = str(getattr(exc, "type", "") or "")
        said = getattr(exc, "api_message", None) or getattr(exc, "message", "") or str(exc)
        detail = scrub(f"HTTP {status}: {said}", self._api_key)[:300]
        code = str(getattr(exc, "error_code", "") or "")
        text = detail.lower()
        if code in ACCOUNT_HOLD_CODES:
            return ClaudeError(
                "The Claude API organization is on hold.",
                hint="Check the organization in the Claude Console; nothing done so far is lost.",
                status=status,
                detail=detail,
                stop_run=True,
            )
        if status == 401:
            return ClaudeError(
                "The Claude API key was not accepted.",
                hint="Run 'notebook-alchemy setup' and enter a current key.",
                status=status,
                detail=detail,
                stop_run=True,
            )
        if (
            status == 402
            or kind == "billing_error"
            or code in SPEND_LIMIT_CODES
            or any(w in text for w in _LIMIT_WORDS)
        ):
            return ClaudeError(
                "The Claude API account has no credit left, or its spend limit is reached.",
                hint="Check Billing and Limits in the Claude Console; nothing done so far is lost, "
                "and the same command continues from here.",
                status=status,
                detail=detail,
                stop_run=True,
            )
        if status == 403:
            return ClaudeError(
                "The Claude API key is not allowed to make this request.",
                hint="Check the key's workspace in the Claude Console, or run 'notebook-alchemy setup'.",
                status=status,
                detail=detail,
                stop_run=True,
            )
        if status == 429:
            # A workspace limit set for this model (0 blocks it) never clears by waiting.
            configured = "configured" in text or self.model != DEFAULT_MODEL
            return ClaudeError(
                "The Claude API rate limit is still reached after waiting.",
                hint=(
                    f"The workspace may limit '{self.model}': raise its limit in the Claude Console, or "
                    "unset ALCHEMY_MODEL to use the default model. What was done is kept."
                    if configured
                    else "Run the same command again later; what was done is kept."
                ),
                status=status,
                detail=detail,
                stop_run=True,
            )
        if status == 404:
            return ClaudeError(
                f"The model '{self.model}' is not available to this key.",
                hint="Unset ALCHEMY_MODEL to use the default model.",
                status=status,
                detail=detail,
                stop_run=True,
            )
        return ClaudeError(
            f"The Claude API request failed ({detail}).",
            hint="Run the same command again; what was done is kept. If it keeps failing with another "
            "model, unset ALCHEMY_MODEL to use the default model.",
            status=status,
            detail=detail,
        )

    # -- usage --

    def _record_usage(self, result: ClaudeResult, *, batch: bool) -> None:
        price_in, price_out, _note = price_for(result.model or self.model, result.input_tokens)
        factor = BATCH_PRICE_FACTOR if batch else 1.0
        cost = (result.input_tokens * price_in + result.output_tokens * price_out) / 1e6 * factor
        with self._usage_lock:
            row = self._usage.setdefault(
                (result.model or self.model) + (BATCH_SUFFIX if batch else ""), [0, 0, 0.0]
            )
            row[0] += result.input_tokens
            row[1] += result.output_tokens
            row[2] += cost

    def usage(self) -> list[tuple[str, int, int, float]]:
        """(model, input tokens, output tokens, USD at the listed price) for this run's answers."""
        with self._usage_lock:
            return [(m, int(r[0]), int(r[1]), float(r[2])) for m, r in sorted(self._usage.items())]

    # -- batches: half price, asynchronous --

    def submit_batch(self, requests: list[tuple[str, str, dict[str, Any], str | None, int]]) -> str:
        """Submit (key, prompt, schema, system, max_tokens) requests as one batch; return its id.

        Requests are numbered r0, r1, ... in the order given; `batch_results` takes the same keys
        in the same order.
        """
        body = [
            {
                "custom_id": f"r{i}",
                "params": request_params(self.model, prompt, schema, system=system, max_tokens=max_tokens),
            }
            for i, (_key, prompt, schema, system, max_tokens) in enumerate(requests)
        ]
        try:
            batch = self._client.messages.batches.create(requests=body)
        except anthropic.APIStatusError as exc:
            raise self._status_error(exc) from None
        except anthropic.APIConnectionError as exc:
            raise ClaudeError(
                "Could not reach the Claude API.", detail=scrub(f"connection: {exc}", self._api_key)
            ) from None
        return str(batch.id)

    def cancel_batch(self, batch_id: str) -> None:
        """Stop a batch whose answers are no longer wanted, so it does not go on billing."""
        try:
            self._client.messages.batches.cancel(batch_id)
        except anthropic.APIStatusError as exc:
            if int(getattr(exc, "status_code", 0) or 0) == 404:
                return  # already gone
            raise self._status_error(exc) from None
        except anthropic.APIConnectionError as exc:
            raise ClaudeError(
                "Could not reach the Claude API.", detail=scrub(f"connection: {exc}", self._api_key)
            ) from None

    def batch_results(
        self, batch_id: str, keys: list[str]
    ) -> tuple[str, dict[str, ClaudeResult | ClaudeError] | None]:
        """(processing status, results by key once the batch has ended, else None).

        A batch the API no longer knows (its results expire after 29 days, or another key's workspace
        made it) ends as "gone" with no results, so the caller asks again instead of waiting forever.
        """
        try:
            batch = self._client.messages.batches.retrieve(batch_id)
            status = str(batch.processing_status)
            if status != "ended":
                return status, None
            out: dict[str, ClaudeResult | ClaudeError] = {}
            for item in self._client.messages.batches.results(batch_id):
                match = _CUSTOM_ID.match(str(item.custom_id))
                index = int(match.group(1)) if match else -1
                if not 0 <= index < len(keys):
                    logger.warning("Ignoring a batch result with an unknown id %r", item.custom_id)
                    continue
                out[keys[index]] = self._batch_item(item.result)
        except anthropic.APIStatusError as exc:
            if int(getattr(exc, "status_code", 0) or 0) == 404:
                return "gone", {}
            raise self._status_error(exc) from None
        except anthropic.APIConnectionError as exc:
            raise ClaudeError(
                "Could not reach the Claude API.", detail=scrub(f"connection: {exc}", self._api_key)
            ) from None
        return status, out

    def _batch_item(self, result: Any) -> ClaudeResult | ClaudeError:
        kind = str(getattr(result, "type", ""))
        if kind == "succeeded":
            try:
                return self._result(result.message, batch=True)
            except ClaudeError as exc:
                return exc
        if kind == "errored":
            error = getattr(getattr(result, "error", None), "error", None)
            etype = str(getattr(error, "type", "") or "error")
            message = scrub(str(getattr(error, "message", "") or ""), self._api_key)[:200]
            return ClaudeError(
                f"A batch request failed ({etype}).", detail=f"{etype}: {message}" if message else etype
            )
        return ClaudeError(f"A batch request was {kind or 'not answered'}.", detail=kind or "unanswered")
