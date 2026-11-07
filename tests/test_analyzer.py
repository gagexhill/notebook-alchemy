"""Source analysis: discovery, clustering and cost estimates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notebook_alchemy.analyzer import (
    MISC_DOMAIN,
    NotebookAnalyzer,
    estimate_video_tokens,
)
from notebook_alchemy.client import NotebookLMClient
from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.errors import GeminiError, NotebookLMError, SessionError
from notebook_alchemy.gemini import GeminiResult

KEY = "K" * 24


class FakeGemini:
    """Scripted Gemini: `domains` for stage 1; `assign(start, count)` builds stage-2 answers."""

    model = "fake"

    def __init__(self, domains=None, assign=None, fail_at: int | None = None) -> None:
        self.domains = domains or ["Alpha Topics", "Beta Topics", "Gamma Topics"]
        self.assign = assign or (lambda idx, k: idx % k)
        self.fail_at = fail_at
        self.prompts: list[str] = []
        self.notices: list[str] = []

    def drain_notices(self) -> list[str]:
        notices, self.notices = self.notices, []
        return notices

    def generate(self, prompt: str, **kwargs: object) -> GeminiResult:
        self.prompts.append(prompt)
        if self.fail_at is not None and len(self.prompts) == self.fail_at:
            raise GeminiError("boom")
        if "Name " in prompt and "subject domains" in prompt:
            return GeminiResult(json.dumps({"domains": self.domains}), "STOP")
        numbers = [
            int(line.split(":", 1)[0])
            for line in prompt.split("Titles:\n")[1].splitlines()
            if ":" in line and line.split(":", 1)[0].isdigit()
        ]
        mapping = {str(n): self.assign(n, len(self.domains)) for n in numbers}
        return GeminiResult(json.dumps({"assignments": mapping}), "STOP")


def _analyzer(gemini=None, key: str = KEY, offline: bool = False, client=None) -> NotebookAnalyzer:
    return NotebookAnalyzer(
        AlchemyConfig(gemini_api_key=key), force_offline=offline, gemini=gemini, client=client
    )


# --- explicit mock only ---------------------------------------------------------------------------


def test_mock_requires_the_flag() -> None:
    analyzer = _analyzer(key="")
    with pytest.raises(SessionError):  # not a URL, no NotebookLM session: no silent sample data
        analyzer.generate_dry_run("Example Notebook")
    report = analyzer.generate_dry_run("anything", use_mock=True)
    assert report.is_mock and report.origin == "mock"
    assert len(report.youtube_videos) == 288


def test_unknown_notebook_is_an_error(tmp_path) -> None:
    state = Path.home() / ".notebooklm" / "profiles" / "default" / "storage_state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    config = AlchemyConfig(state_dir=tmp_path / "state", gemini_api_key="")
    client = NotebookLMClient(
        config, runner=lambda cmd, **kw: __import__("subprocess").CompletedProcess(cmd, 0, "[]", "")
    )
    analyzer = NotebookAnalyzer(config, client=client)
    with pytest.raises(NotebookLMError, match="No notebook matches"):
        analyzer.generate_dry_run("Missing Notebook")


def test_limit_applies_to_mock() -> None:
    assert len(_analyzer(key="").generate_dry_run("x", use_mock=True, limit=10).youtube_videos) == 10


# --- estimates ------------------------------------------------------------------------------------


def test_tokens_follow_duration() -> None:
    assert estimate_video_tokens(0) == 4500
    assert estimate_video_tokens(600) == 1980
    assert estimate_video_tokens(1) == 200  # floor


def test_cost_uses_the_model_price_table() -> None:
    analyzer = _analyzer(key="")
    analyzer.model = "gemini-2.5-flash-lite"
    report = analyzer.generate_dry_run("x", use_mock=True)
    # 288 sample videos have no duration, so 4,500 tokens each at $0.10 per 1M for flash-lite
    assert report.total_estimated_tokens == 288 * 4500
    assert report.estimated_input_cost_usd == round(288 * 4500 / 1e6 * 0.10, 4)
    assert report.estimated_synthesis_output_cost_usd == round(288 * 500 / 1e6 * 0.40, 4)


def test_unknown_model_adds_a_note() -> None:
    analyzer = _analyzer(key="")
    analyzer.model = "gemini-unlisted"
    assert any("gemini-unlisted" in n for n in analyzer.generate_dry_run("x", use_mock=True).notes)


# --- keyword fallback -----------------------------------------------------------------------------


def test_keywords_respect_word_boundaries() -> None:
    clusters = _analyzer(key="").cluster_titles(
        [
            "I Asked NASA to Use My App Redesign",
            "Said Maintain Paid Air",
            "Building an AI agent",
            "CSS grid tricks",
        ]
    )
    flat = {t: d for d, ts in clusters.items() for t in ts}
    assert flat["Building an AI agent"] == "AI, LLMs & Agentic Coding"
    assert flat["CSS grid tricks"] == "Frontend & Modern UI Engineering"
    assert flat["I Asked NASA to Use My App Redesign"] == "General / Specialized Topics"
    assert flat["Said Maintain Paid Air"] == "General / Specialized Topics"


def test_no_key_note_and_offline_flag() -> None:
    analyzer = _analyzer(key="")
    analyzer.cluster_titles(["a", "b"])
    assert analyzer.cluster_method == "keyword"
    assert any("No Gemini API key" in n for n in analyzer.notes)
    offline = _analyzer(FakeGemini(), offline=True)
    offline.cluster_titles(["a"])
    assert offline.cluster_method == "keyword" and not offline.notes


# --- Gemini clustering ----------------------------------------------------------------------------


def test_every_title_is_assigned_beyond_300() -> None:
    titles = [f"Video number {i}" for i in range(1000)]
    fake = FakeGemini()
    analyzer = _analyzer(fake)
    clusters = analyzer.cluster_titles(titles)
    assert analyzer.cluster_method == "gemini"
    assert sum(len(v) for v in clusters.values()) == 1000
    assert sorted(t for v in clusters.values() for t in v) == sorted(titles)
    # stage 1 saw a bounded sample; stage 2 batched all titles
    assert fake.prompts[0].count("\n- ") == 300
    assert len(fake.prompts) == 1 + 7  # ceil(1000 / 150) assignment batches


def test_unassigned_titles_go_to_misc_with_a_warning() -> None:
    fake = FakeGemini(assign=lambda idx, k: idx % k if idx % 5 else 99)  # every 5th is invalid
    analyzer = _analyzer(fake)
    clusters = analyzer.cluster_titles([f"T{i}" for i in range(50)])
    assert len(clusters[MISC_DOMAIN]) == 10
    assert sum(len(v) for v in clusters.values()) == 50
    assert any("no valid assignment" in n for n in analyzer.notes)


def test_duplicate_titles_keep_distinct_videos() -> None:
    analyzer = _analyzer(FakeGemini())
    report = analyzer.generate_dry_run("x", use_mock=True)  # warm path only
    assert report.cluster_method == "gemini"
    from notebook_alchemy.analyzer import VideoSourceSummary

    same = [VideoSourceSummary(f"id{i}", "Same Title", f"https://u/{i}", "yt", "ready") for i in range(4)]
    indices = analyzer._cluster_indices([v.title for v in same])
    assert sorted(i for ids in indices.values() for i in ids) == [0, 1, 2, 3]


@pytest.mark.parametrize("fail_at", [1, 2])
def test_gemini_failure_falls_back_with_a_note(fail_at: int) -> None:
    analyzer = _analyzer(FakeGemini(fail_at=fail_at))
    clusters = analyzer.cluster_titles(["Build a REST API", "CSS grid"])
    assert analyzer.cluster_method == "keyword"
    assert any("Gemini clustering unavailable" in n for n in analyzer.notes)
    assert sum(len(v) for v in clusters.values()) == 2


def test_bad_json_falls_back() -> None:
    class Garbage(FakeGemini):
        def generate(self, prompt, **kwargs):
            return GeminiResult("not json", "STOP")

    analyzer = _analyzer(Garbage())
    analyzer.cluster_titles(["x"])
    assert analyzer.cluster_method == "keyword"
    assert any("unusable answer" in n for n in analyzer.notes)


def test_too_few_domains_falls_back() -> None:
    analyzer = _analyzer(FakeGemini(domains=["Only One", "Two"]))
    analyzer.cluster_titles(["x"])
    assert analyzer.cluster_method == "keyword"


def test_fallback_model_notice_reaches_the_report() -> None:
    fake = FakeGemini()
    fake.notices = ["gemini-3.8-flash was unavailable (HTTP 503); used gemini-3.5-flash-lite."]
    analyzer = _analyzer(fake)
    analyzer.cluster_titles(["a", "b", "c"])
    assert analyzer.cluster_method == "gemini"
    assert any("used gemini-3.5-flash-lite" in n for n in analyzer.notes)


def test_fallback_notice_is_kept_even_when_clustering_fails() -> None:
    fake = FakeGemini(fail_at=1)
    fake.notices = ["gemini-3.8-flash was unavailable (HTTP 503); used gemini-3.5-flash-lite."]
    analyzer = _analyzer(fake)
    analyzer.cluster_titles(["a"])
    assert analyzer.cluster_method == "keyword"
    assert any("used gemini-3.5-flash-lite" in n for n in analyzer.notes)


def test_estimated_gemini_requests_cover_discovery_assignment_and_each_volume() -> None:
    report = _analyzer(key="").generate_dry_run("x", use_mock=True)  # 288 videos, keyword domains
    volumes = sum(-(-len(v) // 50) for v in report.domain_videos.values())
    assert report.estimated_gemini_requests == 1 + 2 + volumes  # 288 titles = 2 assignment batches
