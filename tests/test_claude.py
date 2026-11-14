"""The Claude client: request settings, stop reasons, error mapping, the key scrub, batches, cost."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import pytest

from notebook_alchemy.claude import (
    BATCH_PRICE_FACTOR,
    ClaudeClient,
    ClaudeResult,
    price_for,
    scrub,
)
from notebook_alchemy.errors import ClaudeError

# Synthetic: the shape of a key, marked as a test value, valid nowhere.
KEY = "sk-ant-TEST-ONLY-synthetic-key-0000"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def _message(text: str = '{"ok": true}', stop: str = "end_turn", tokens: tuple[int, int] = (1000, 100)):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
        model="claude-haiku-5-5",
    )


def _status_error(status: int, kind: str, message: str, code: str = "") -> anthropic.APIStatusError:
    # The SDK reads only these from a response, so its HTTP library is not needed here.
    response = SimpleNamespace(request=None, status_code=status, headers={})
    error: dict = {"type": kind, "message": message}
    if code:
        error["details"] = {"error_code": code}
    body = {"type": "error", "error": error}
    return anthropic.APIStatusError(message, response=response, body=body)  # type: ignore[arg-type]


class FakeMessages:
    def __init__(self, answers: list) -> None:
        self.answers = answers
        self.sent: list[dict] = []
        self.batches = FakeBatches()

    def create(self, **params):
        self.sent.append(params)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[dict]] = []
        self.status = "in_progress"
        self.items: list = []

    def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id="msgbatch_test")

    def retrieve(self, batch_id):
        return SimpleNamespace(processing_status=self.status)

    def results(self, batch_id):
        return iter(self.items)


def _client(*answers) -> tuple[ClaudeClient, FakeMessages]:
    messages = FakeMessages(list(answers))
    return ClaudeClient(KEY, "claude-haiku-5-5", client=SimpleNamespace(messages=messages)), messages


def test_a_request_asks_for_the_schema_with_thinking_off_and_low_effort() -> None:
    client, messages = _client(_message())
    result = client.generate("prompt", schema=SCHEMA, system="rules", max_tokens=500)
    sent = messages.sent[0]
    assert sent["thinking"] == {"type": "disabled"}
    assert sent["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert sent["system"] == "rules" and sent["max_tokens"] == 500 and sent["model"] == "claude-haiku-5-5"
    assert json.loads(result.text) == {"ok": True}


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "pause_turn"])
def test_an_answer_that_did_not_end_normally_is_unusable_and_still_counted(stop: str) -> None:
    client, _ = _client(_message(stop=stop))
    with pytest.raises(ClaudeError) as caught:
        client.generate("prompt", schema=SCHEMA)
    assert caught.value.unusable and not caught.value.stop_run
    assert client.usage()[0][1:3] == (1000, 100)  # billed, so it is counted


@pytest.mark.parametrize(
    ("status", "kind", "message", "stops"),
    [
        (401, "authentication_error", "invalid x-api-key", True),
        (402, "billing_error", "payment required", True),
        (400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API.", True),
        (400, "invalid_request_error", "You have reached your specified workspace API usage limits.", True),
        (403, "permission_error", "not allowed", True),
        (404, "not_found_error", "model: claude-nope", True),
        (429, "rate_limit_error", "would exceed the rate limit you configured in workspace", True),
        (500, "api_error", "internal error", False),
        (529, "overloaded_error", "overloaded", False),
        (400, "invalid_request_error", "messages: field required", False),
    ],
)
def test_errors_that_would_fail_every_request_stop_the_run(status, kind, message, stops) -> None:
    client, _ = _client(_status_error(status, kind, message))
    with pytest.raises(ClaudeError) as caught:
        client.generate("prompt", schema=SCHEMA)
    assert caught.value.stop_run is stops and caught.value.status == status


def test_the_key_never_appears_in_an_error() -> None:
    client, _ = _client(_status_error(401, "authentication_error", f"bad key {KEY}"))
    with pytest.raises(ClaudeError) as caught:
        client.generate("prompt", schema=SCHEMA)
    shown = f"{caught.value.message} {caught.value.hint} {caught.value.detail}"
    assert KEY not in shown and "bad key ***" in shown
    assert scrub(f"x {KEY} y") == "x sk-ant-*** y" and scrub("key=abc", "abc") == "key=***"


def test_a_batch_numbers_its_requests_and_maps_answers_back_by_key() -> None:
    client, messages = _client()
    keys = ["video#0", "video#1", "other#0"]
    batch_id = client.submit_batch([(k, f"prompt {k}", SCHEMA, "rules", 300) for k in keys])
    created = messages.batches.created[0]
    assert batch_id == "msgbatch_test" and [r["custom_id"] for r in created] == ["r0", "r1", "r2"]
    assert created[1]["params"]["output_config"]["effort"] == "low"  # same settings as a direct call
    assert client.batch_results(batch_id, keys) == ("in_progress", None)

    def item(cid: str, result):
        return SimpleNamespace(custom_id=cid, result=result)

    error = SimpleNamespace(error=SimpleNamespace(type="invalid_request_error", message="bad"))
    messages.batches.items = [
        item("r2", SimpleNamespace(type="succeeded", message=_message('{"ok": false}'))),
        item("r0", SimpleNamespace(type="errored", error=error)),
        item("r1", SimpleNamespace(type="expired")),
        item("r9", SimpleNamespace(type="succeeded", message=_message())),  # not ours: ignored
    ]
    messages.batches.status = "ended"
    status, results = client.batch_results(batch_id, keys)
    assert status == "ended" and results is not None
    assert isinstance(results["other#0"], ClaudeResult) and json.loads(results["other#0"].text) == {
        "ok": False
    }
    assert (
        isinstance(results["video#0"], ClaudeError) and "invalid_request_error" in results["video#0"].detail
    )
    assert isinstance(results["video#1"], ClaudeError) and not results["video#1"].unusable
    assert [row[0] for row in client.usage()] == ["claude-haiku-5-5 (batch)"]  # only the answered one


def test_cost_uses_the_listed_rates_and_halves_for_batches() -> None:
    price_in, price_out, note = price_for("claude-haiku-5-5")
    assert note is None and price_out > price_in > 0
    long_in, _long_out, _ = price_for("claude-haiku-5-5", prompt_tokens=200_000)
    assert long_in > price_in  # prompts over 100K tokens bill at the higher rate
    assert price_for("claude-unlisted")[2]  # an unlisted model is flagged
    client, messages = _client(_message(tokens=(100_000, 0)))
    client.generate("prompt", schema=SCHEMA)
    standard = client.usage()[0][3]
    assert standard == pytest.approx(price_in * 0.1)  # 100K tokens at the per-1M rate
    messages.batches.items = [
        SimpleNamespace(
            custom_id="r0", result=SimpleNamespace(type="succeeded", message=_message(tokens=(100_000, 0)))
        )
    ]
    messages.batches.status = "ended"
    client.batch_results("msgbatch_test", ["k"])
    batch = dict((m, c) for m, _i, _o, c in client.usage())["claude-haiku-5-5 (batch)"]
    assert batch == pytest.approx(standard * BATCH_PRICE_FACTOR)


def test_a_connection_failure_is_a_per_request_failure() -> None:
    client, _ = _client(anthropic.APIConnectionError(request=None))  # type: ignore[arg-type]
    with pytest.raises(ClaudeError) as caught:
        client.generate("prompt", schema=SCHEMA)
    assert not caught.value.stop_run and caught.value.status is None


@pytest.mark.parametrize(
    ("status", "code", "says"),
    [
        (429, "enforced_spend_limit_reached", "spend limit"),  # the stable code, whatever the wording
        (400, "organization_on_hold", "on hold"),
    ],
)
def test_a_stable_error_code_stops_the_run_with_its_own_reason(status: int, code: str, says: str) -> None:
    client, _ = _client(_status_error(status, "invalid_request_error", "something happened", code=code))
    with pytest.raises(ClaudeError) as caught:
        client.generate("prompt", schema=SCHEMA)
    assert caught.value.stop_run and says in caught.value.message


def test_a_rate_limit_set_on_the_workspace_says_how_to_lift_it() -> None:
    client, _ = _client(_status_error(429, "rate_limit_error", "would exceed the rate limit you configured"))
    with pytest.raises(ClaudeError) as caught:
        client.generate("prompt", schema=SCHEMA)
    assert "ALCHEMY_MODEL" in (caught.value.hint or "")
    plain, _ = _client(_status_error(429, "rate_limit_error", "rate limited"))
    with pytest.raises(ClaudeError) as caught:
        plain.generate("prompt", schema=SCHEMA)
    assert "again later" in (caught.value.hint or "")


def test_any_other_failure_names_its_reason_and_a_next_step() -> None:
    client, _ = _client(_status_error(400, "invalid_request_error", "thinking: not supported for this model"))
    with pytest.raises(ClaudeError) as caught:
        client.generate("prompt", schema=SCHEMA)
    assert "thinking: not supported" in caught.value.message and caught.value.hint


def test_a_batch_the_api_no_longer_knows_ends_as_gone_and_can_be_cancelled() -> None:
    client, messages = _client()

    def missing(batch_id):
        raise _status_error(404, "not_found_error", "batch not found")

    messages.batches.retrieve = missing
    assert client.batch_results("msgbatch_old", ["k"]) == ("gone", {})
    cancelled: list[str] = []
    messages.batches.cancel = cancelled.append
    client.cancel_batch("msgbatch_old")
    assert cancelled == ["msgbatch_old"]
