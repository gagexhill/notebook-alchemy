"""Labelling: prompts, parsing, the per-video cache, run-stopping limits and batches."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from notebook_alchemy.claude import ClaudeResult
from notebook_alchemy.cleanup import Chunk
from notebook_alchemy.errors import ClaudeError
from notebook_alchemy.knowledge import (
    KnowledgeStore,
    Labeler,
    VideoText,
    label_key,
    label_prompt,
    label_schema,
    parse_labels,
    windows,
)

TODAY = "2026-11-08"


def _video(vid: str = "abcdefghijk", n: int = 6, transcript: str = "words") -> VideoText:
    chunks = [
        Chunk(vid, i, f"chunk {i} says something specific about SAM.gov fee {i * 10} dollars")
        for i in range(1, n + 1)
    ]
    return VideoText(vid, f"Title {vid}", "2024-05-01", transcript + vid, chunks)


def _labels_for(prompt: str, keep_all: bool = False) -> str:
    """Drop the first chunk (an intro), keep the rest as one point."""
    nums = [int(n) for n in re.findall(r"^\[(\d+)\]", prompt, re.MULTILINE)]
    first, last = nums[0], nums[-1]
    segs = []
    if not keep_all and last > first:
        segs.append({"from": first, "to": first, "keep": False})
        first += 1
    segs.append({"from": first, "to": last, "keep": True, "headline": "Fees", "kind": "figure", "note": ""})
    return json.dumps({"segments": segs})


LIMIT = ClaudeError("spend limit", status=400, detail="workspace usage limit reached", stop_run=True)


class FakeAI:
    """Stands in for ClaudeClient: answers label requests, and batches when asked."""

    model = "claude-test"

    def __init__(
        self, *, limit_after: int | None = None, batch: str = "failed", drop_all: bool = False
    ) -> None:
        self.calls: list[str] = []
        self.attempts = 0  # every request made, answered or not
        self.cancelled: list[str] = []
        self.unusable_on: str | None = None  # a prompt phrase answered with a refusal
        self.schemas: list[dict] = []
        self.limit_after = limit_after
        self.batch = batch
        self.drop_all = drop_all
        self.batches: list[list[str]] = []
        self.polls = 0

    def generate(self, prompt: str, *, schema: dict, **kw) -> ClaudeResult:
        self.attempts += 1
        if self.limit_after is not None and len(self.calls) >= self.limit_after:
            raise LIMIT
        if self.unusable_on and self.unusable_on in prompt:
            self.calls.append(prompt)
            raise ClaudeError(
                "Unusable answer.", detail="Claude declined to answer this request", unusable=True
            )
        self.calls.append(prompt)
        self.schemas.append(schema)
        if self.drop_all and "almost everything was dropped" not in prompt:
            nums = re.findall(r"^\[(\d+)\]", prompt, re.MULTILINE)
            return ClaudeResult(
                json.dumps({"segments": [{"from": int(nums[0]), "to": int(nums[-1]), "keep": False}]}),
                "end_turn",
            )
        return ClaudeResult(_labels_for(prompt), "end_turn")

    def submit_batch(self, requests):
        if self.batch == "failed":
            raise ClaudeError("batch", status=500, detail="HTTP 500: internal error")
        if self.batch == "limit":
            raise LIMIT
        self.batches.append([r[0] for r in requests])
        self._prompts = {r[0]: r[1] for r in requests}
        return "msgbatch_1"

    def cancel_batch(self, batch_id):
        self.cancelled.append(batch_id)

    def batch_results(self, batch_id, keys):
        self.polls += 1
        if self.batch == "gone":
            return "gone", {}
        if self.batch == "running":
            return "in_progress", None
        out: dict = {}
        for k in keys:
            if self.batch == "one_errored" and k == keys[0]:
                out[k] = ClaudeError("batch item", detail="api_error: overloaded")
                continue
            if self.batch == "one_refused" and k == keys[0]:
                out[k] = ClaudeError("Unusable answer.", detail="refused", unusable=True)
                continue
            out[k] = ClaudeResult(_labels_for(self._prompts[k]), "end_turn")
        return "ended", out


def _labeler(tmp_path: Path, ai: FakeAI, **kw) -> Labeler:
    return Labeler(
        ai,
        KnowledgeStore(tmp_path / "knowledge"),
        TODAY,
        state_path=tmp_path / "batch.json",
        sleep=lambda s: None,
        **kw,
    )


def test_the_prompt_carries_the_rules_today_and_numbered_chunks() -> None:
    prompt = label_prompt("T", "2023-02-01", TODAY, _video().chunks)
    assert "Today: 2026-11-08" in prompt and "Published: 2023-02-01" in prompt
    assert "sponsor reads" in prompt and "When unsure whether something is useful, KEEP it." in prompt
    assert "[1] chunk 1" in prompt and "chunks 1 to 6" in prompt


def test_labels_outside_the_chunks_or_overlapping_are_dropped_and_counted() -> None:
    answer = json.dumps(
        {
            "segments": [
                {"from": 1, "to": 3, "keep": True, "headline": "A " * 40, "kind": "weird"},
                {"from": 3, "to": 4, "keep": True},  # overlaps 3
                {"from": 9, "to": 12, "keep": True},  # outside
                {"from": "x", "to": 5},
                {"from": 5, "to": 6, "keep": False, "headline": "dropped ones carry no headline"},
            ]
        }
    )
    segs, invalid = parse_labels(answer, {1, 2, 3, 4, 5, 6})
    assert invalid == 3 and [(s.start, s.end, s.keep) for s in segs] == [(1, 3, True), (5, 6, False)]
    assert len(segs[0].headline.split()) == 14 and segs[0].kind == "lasting" and segs[1].headline == ""
    with pytest.raises(ValueError):
        parse_labels('{"nope": 1}', {1})


def test_a_video_is_labelled_once_then_read_from_the_cache(tmp_path: Path) -> None:
    ai = FakeAI()
    first = _labeler(tmp_path, ai).run([_video()])
    assert first.labelled_now == 1 and len(ai.calls) == 1
    labels = first.labels["abcdefghijk"]
    assert [(s.start, s.end, s.keep) for s in labels.segments] == [(1, 1, False), (2, 6, True)]
    again = _labeler(tmp_path, ai).run([_video()])
    assert again.labelled_now == 0 and len(ai.calls) == 1  # no request the second time


def test_a_changed_transcript_or_model_labels_again(tmp_path: Path) -> None:
    ai = FakeAI()
    _labeler(tmp_path, ai).run([_video()])
    _labeler(tmp_path, ai).run([_video(transcript="edited")])
    assert len(ai.calls) == 2
    assert label_key("v", "t", "m1") != label_key("v", "t", "m2")


def test_a_spend_limit_keeps_what_was_done_and_leaves_the_rest_for_next_run(tmp_path: Path) -> None:
    ai = FakeAI(limit_after=2)
    run = _labeler(tmp_path, ai, workers=1).run([_video(f"vid{i:08d}") for i in range(4)])
    assert run.labelled_now == 2 and len(run.waiting) == 2
    assert any("spend limit" in n and "wait for the next run" in n for n in run.notices)
    assert ai.attempts == 3  # the third request met the limit; the fourth was never made
    resumed = _labeler(tmp_path, FakeAI(), workers=1).run([_video(f"vid{i:08d}") for i in range(4)])
    assert resumed.labelled_now == 2 and not resumed.waiting


def test_almost_everything_dropped_is_labelled_once_more_keeping_when_unsure(tmp_path: Path) -> None:
    ai = FakeAI(drop_all=True)
    labels = _labeler(tmp_path, ai).run([_video()]).labels["abcdefghijk"]
    assert labels.relabelled and any(s.keep for s in labels.segments) and len(ai.calls) == 2


def test_a_batch_that_cannot_start_falls_back_to_standard_calls_and_says_so(tmp_path: Path) -> None:
    ai = FakeAI(batch="failed")
    run = _labeler(tmp_path, ai).run([_video()])
    assert run.labelled_now == 1 and len(ai.calls) == 1
    assert any("could not be started" in n for n in run.notices)


def test_a_spend_limit_on_the_batch_stops_the_run_instead_of_paying_full_price(tmp_path: Path) -> None:
    ai = FakeAI(batch="limit")
    with pytest.raises(ClaudeError) as caught:
        _labeler(tmp_path, ai).run([_video()])
    assert caught.value.stop_run and ai.calls == []


def test_a_batch_request_that_was_not_answered_is_asked_again_directly(tmp_path: Path) -> None:
    ai = FakeAI(batch="one_errored")
    run = _labeler(tmp_path, ai).run([_video("vid00000001"), _video("vid00000002")])
    assert run.labelled_now == 2 and len(ai.calls) == 1  # only the unanswered one, at full price
    assert any("were not answered" in n for n in run.notices)


def test_each_request_asks_for_the_label_shape_with_text_only_when_punctuating(tmp_path: Path) -> None:
    ai = FakeAI()
    _labeler(tmp_path, ai).run([_video(transcript="Punctuated. Sentences here. ")])
    _labeler(tmp_path, ai).run([_video("vid00000009", transcript="no punctuation at all here")])
    first, second = (s["properties"]["segments"]["items"] for s in ai.schemas)
    assert "text" not in first["required"] and "text" in second["required"]
    assert first["additionalProperties"] is False and label_schema()["additionalProperties"] is False


def test_a_finished_batch_labels_without_standard_calls(tmp_path: Path) -> None:
    ai = FakeAI(batch="ok")
    run = _labeler(tmp_path, ai).run([_video("vid00000001"), _video("vid00000002")])
    assert (
        run.labelled_now == 2
        and ai.calls == []
        and len(ai.batches) == 1
        and len(ai.batches[0]) == 2  # one request per video, one batch
    )
    assert not (tmp_path / "batch.json").read_text(encoding="utf-8").strip("{}")  # job cleared


def test_a_batch_still_running_is_remembered_and_collected_next_run(tmp_path: Path) -> None:
    clock = {"t": 0.0}
    running = FakeAI(batch="running")
    first = Labeler(
        running,
        KnowledgeStore(tmp_path / "knowledge"),
        TODAY,
        state_path=tmp_path / "batch.json",
        batch_wait=60,
        poll_every=20,
        sleep=lambda s: clock.update(t=clock["t"] + s),
        clock=lambda: clock["t"],
    ).run([_video()])
    assert first.waiting == ["abcdefghijk"] and any("still running" in n for n in first.notices)
    assert json.loads((tmp_path / "batch.json").read_text(encoding="utf-8"))["job"] == "msgbatch_1"
    finished = FakeAI(batch="ok")
    finished._prompts = running._prompts
    second = _labeler(tmp_path, finished).run([_video()])
    assert second.labelled_now == 1 and finished.batches == []  # collected, not submitted again


def test_long_videos_are_labelled_in_windows_with_numbering_carried_on() -> None:
    chunks = [Chunk("v", i, "word " * 500) for i in range(1, 400)]
    parts = windows(chunks)
    assert len(parts) == 3 and parts[1][0].n == parts[0][-1].n + 1


def test_batch_requests_are_tied_to_the_transcript() -> None:
    from notebook_alchemy.knowledge import Labeler

    labeler = Labeler(FakeAI(), KnowledgeStore(Path("unused")), TODAY, state_path=Path("unused"))
    same = labeler._requests(_video())[0][0]
    assert same == labeler._requests(_video())[0][0] != labeler._requests(_video(transcript="edited"))[0][0]


def test_a_relabel_that_fails_leaves_the_video_waiting_instead_of_saving_poor_labels(tmp_path: Path) -> None:
    class DropsThenLimit(FakeAI):
        def generate(self, prompt: str, **kw) -> ClaudeResult:
            if "almost everything was dropped" in prompt:
                raise LIMIT
            return super().generate(prompt, **kw)

    ai = DropsThenLimit(drop_all=True)
    run = _labeler(tmp_path, ai).run([_video()])
    assert run.waiting == ["abcdefghijk"] and not list((tmp_path / "knowledge").glob("*.json"))


def test_labels_record_which_chunks_the_model_saw(tmp_path: Path) -> None:
    labels = _labeler(tmp_path, FakeAI()).run([_video(n=4)]).labels["abcdefghijk"]
    assert labels.seen == [1, 2, 3, 4] and labels.covers(3) and not labels.covers(9)


def test_a_refused_answer_is_saved_as_no_labels_and_not_paid_for_again(tmp_path: Path) -> None:
    ai = FakeAI()
    ai.unusable_on = "Title vid00000001"
    run = _labeler(tmp_path, ai).run([_video("vid00000001"), _video("vid00000002")])
    assert run.labelled_now == 2 and not run.waiting  # nothing waits, so the upload is not blocked
    assert run.labels["vid00000001"].segments == [] and any("could not be used" in n for n in run.notices)
    again = FakeAI()
    _labeler(tmp_path, again).run([_video("vid00000001"), _video("vid00000002")])
    assert again.attempts == 0  # both came from the cache


def test_a_refused_batch_answer_is_not_asked_again_directly(tmp_path: Path) -> None:
    ai = FakeAI(batch="one_refused")
    run = _labeler(tmp_path, ai).run([_video("vid00000001"), _video("vid00000002")])
    assert run.labelled_now == 2 and ai.attempts == 0


def test_a_batch_for_other_videos_is_stopped_before_a_new_one_starts(tmp_path: Path) -> None:
    (tmp_path / "batch.json").write_text(
        json.dumps({"job": "msgbatch_old", "keys": ["someone-else#0"], "model": "claude-test"}),
        encoding="utf-8",
    )
    ai = FakeAI(batch="ok")
    run = _labeler(tmp_path, ai).run([_video()])
    assert ai.cancelled == ["msgbatch_old"] and run.labelled_now == 1
    assert any("it was stopped" in n for n in run.notices)


def test_a_batch_that_is_gone_is_asked_again_directly(tmp_path: Path) -> None:
    (tmp_path / "batch.json").write_text(
        json.dumps({"job": "msgbatch_old", "keys": [], "model": "claude-test"}), encoding="utf-8"
    )
    ai = FakeAI(batch="gone")
    run = _labeler(tmp_path, ai).run([_video()])
    assert (
        run.labelled_now == 1 and len(ai.calls) == 1 and any("no longer available" in n for n in run.notices)
    )
    assert not (tmp_path / "batch.json").read_text(encoding="utf-8").strip("{}")
