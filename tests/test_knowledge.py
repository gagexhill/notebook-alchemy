"""Labelling: prompts, parsing, the per-video cache, quotas and batch mode."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from notebook_alchemy.cleanup import Chunk
from notebook_alchemy.errors import GeminiError
from notebook_alchemy.gemini import GeminiResult
from notebook_alchemy.knowledge import (
    KnowledgeStore,
    Labeler,
    VideoText,
    label_key,
    label_prompt,
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


class FakeGemini:
    model = "gemini-test"

    def __init__(
        self, *, quota_after: int | None = None, batch: str = "refused", drop_all: bool = False
    ) -> None:
        self.calls: list[str] = []
        self.quota_after = quota_after
        self.batch = batch
        self.drop_all = drop_all
        self.batches: list[list[str]] = []
        self.polls = 0

    def generate(self, prompt: str, **kw) -> GeminiResult:
        assert kw.get("allow_fallback") is False  # labels are never made by a fallback model
        if self.quota_after is not None and len(self.calls) >= self.quota_after:
            raise GeminiError("quota", status=429, detail="daily request quota exhausted", quota=True)
        self.calls.append(prompt)
        if self.drop_all and "almost everything was dropped" not in prompt:
            nums = re.findall(r"^\[(\d+)\]", prompt, re.MULTILINE)
            return GeminiResult(
                json.dumps({"segments": [{"from": int(nums[0]), "to": int(nums[-1]), "keep": False}]}), "STOP"
            )
        return GeminiResult(_labels_for(prompt), "STOP")

    def submit_batch(self, requests, name):
        if self.batch == "refused":
            raise GeminiError("refused", status=400, detail="HTTP 400: Precondition check failed.")
        self.batches.append([k for k, _ in requests])
        self._bodies = dict(requests)
        return "batches/1"

    def batch_results(self, name, keys):
        self.polls += 1
        if self.batch == "running":
            return "JOB_STATE_RUNNING", None
        out = {}
        for k in keys:
            prompt = self._bodies[k]["contents"][0]["parts"][0]["text"]
            out[k] = GeminiResult(_labels_for(prompt), "STOP")
        return "JOB_STATE_SUCCEEDED", out

    @staticmethod
    def batch_refused(exc: GeminiError) -> bool:
        return "precondition" in exc.detail.lower()


def _labeler(tmp_path: Path, gemini: FakeGemini, **kw) -> Labeler:
    return Labeler(
        gemini,
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
    gemini = FakeGemini()
    first = _labeler(tmp_path, gemini).run([_video()])
    assert first.labelled_now == 1 and len(gemini.calls) == 1
    labels = first.labels["abcdefghijk"]
    assert [(s.start, s.end, s.keep) for s in labels.segments] == [(1, 1, False), (2, 6, True)]
    again = _labeler(tmp_path, gemini).run([_video()])
    assert again.labelled_now == 0 and len(gemini.calls) == 1  # no request the second time


def test_a_changed_transcript_or_model_labels_again(tmp_path: Path) -> None:
    gemini = FakeGemini()
    _labeler(tmp_path, gemini).run([_video()])
    _labeler(tmp_path, gemini).run([_video(transcript="edited")])
    assert len(gemini.calls) == 2
    assert label_key("v", "t", "m1") != label_key("v", "t", "m2")


def test_a_used_up_quota_keeps_what_was_done_and_leaves_the_rest_for_next_run(tmp_path: Path) -> None:
    gemini = FakeGemini(quota_after=2)
    run = _labeler(tmp_path, gemini, workers=1).run([_video(f"vid{i:08d}") for i in range(4)])
    assert run.labelled_now == 2 and len(run.waiting) == 2
    assert any("quota is used up" in n for n in run.notices)
    resumed = _labeler(tmp_path, FakeGemini(), workers=1).run([_video(f"vid{i:08d}") for i in range(4)])
    assert resumed.labelled_now == 2 and not resumed.waiting


def test_almost_everything_dropped_is_labelled_once_more_keeping_when_unsure(tmp_path: Path) -> None:
    gemini = FakeGemini(drop_all=True)
    labels = _labeler(tmp_path, gemini).run([_video()]).labels["abcdefghijk"]
    assert labels.relabelled and any(s.keep for s in labels.segments) and len(gemini.calls) == 2


def test_batch_mode_without_billing_falls_back_to_standard_calls_and_says_so(tmp_path: Path) -> None:
    gemini = FakeGemini(batch="refused")
    run = _labeler(tmp_path, gemini).run([_video()])
    assert run.labelled_now == 1 and len(gemini.calls) == 1
    assert any("needs billing" in n for n in run.notices)


def test_a_finished_batch_labels_without_standard_calls(tmp_path: Path) -> None:
    gemini = FakeGemini(batch="ok")
    run = _labeler(tmp_path, gemini).run([_video("vid00000001"), _video("vid00000002")])
    assert (
        run.labelled_now == 2
        and gemini.calls == []
        and gemini.batches == [["vid00000001#0", "vid00000002#0"]]
    )
    assert not (tmp_path / "batch.json").read_text(encoding="utf-8").strip("{}")  # job cleared


def test_a_batch_still_running_is_remembered_and_collected_next_run(tmp_path: Path) -> None:
    clock = {"t": 0.0}
    running = FakeGemini(batch="running")
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
    assert first.waiting == ["abcdefghijk"] and any("still working" in n for n in first.notices)
    assert json.loads((tmp_path / "batch.json").read_text(encoding="utf-8"))["job"] == "batches/1"
    finished = FakeGemini(batch="ok")
    finished._bodies = running._bodies
    second = _labeler(tmp_path, finished).run([_video()])
    assert second.labelled_now == 1 and finished.batches == []  # collected, not submitted again


def test_long_videos_are_labelled_in_windows_with_numbering_carried_on() -> None:
    chunks = [Chunk("v", i, "word " * 500) for i in range(1, 400)]
    parts = windows(chunks)
    assert len(parts) == 3 and parts[1][0].n == parts[0][-1].n + 1
