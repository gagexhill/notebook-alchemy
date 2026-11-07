"""Compendium generation."""

from __future__ import annotations

import re

import pytest

from notebook_alchemy import __version__
from notebook_alchemy.channel import TranscriptResult
from notebook_alchemy.errors import GeminiError
from notebook_alchemy.gemini import GeminiResult
from notebook_alchemy.synthesizer import (
    SECTION_RELATED,
    Synthesizer,
    notebook_url,
    safe_filename,
    unique_slugs,
    yaml_str,
)

KEY = "K" * 24
UUID = "6fd9a6b5-5065-4c0f-ba7e-711c712fc3c9"


def _videos(n: int) -> list[dict]:
    return [
        {"id": f"id{i:03d}", "title": f"Video {i}", "url": f"https://youtu.be/id{i:03d}"} for i in range(n)
    ]


class FakeGemini:
    model = "fake-model"

    def __init__(self, result: GeminiResult | Exception | None = None) -> None:
        self.result = result or GeminiResult("## Executive Summary\nBody [1].", "STOP", model="fake-model")
        self.prompts: list[str] = []
        self.notices: list[str] = []

    def drain_notices(self) -> list[str]:
        notices, self.notices = self.notices, []
        return notices

    def generate(self, prompt: str, **kwargs: object) -> GeminiResult:
        self.prompts.append(prompt)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeTranscripts:
    def __init__(self, text: str = "spoken words", missing: set[str] | None = None) -> None:
        self.text, self.missing = text, missing or set()

    def fetch_many(self, items, progress=None):
        return {
            vid: (
                TranscriptResult(vid, "", "unavailable", "NoTranscriptFound")
                if vid in self.missing
                else TranscriptResult(vid, self.text, "youtube")
            )
            for vid, _t in items
        }


def test_safe_filename_cases() -> None:
    assert safe_filename("SEO & Local Search: 101!") == "SEO_Local_Search_101"
    assert safe_filename("???") == "domain"
    assert safe_filename("CON").lower() != "con"
    assert len(safe_filename("x" * 300)) <= 80
    assert safe_filename("日本語 CSS") == "日本語_CSS"


def test_colliding_domains_get_distinct_slugs() -> None:
    slugs = unique_slugs(["A & B", "A  B", "A - B", "???", "!!!"])
    assert len({s.lower() for s in slugs.values()}) == 5
    assert slugs["A & B"] == "A_B"


@pytest.mark.parametrize("domain", ["INDEX", "index", "Master Sources Catalog", "MASTER_SOURCES_CATALOG"])
def test_a_domain_cannot_take_a_vault_file_name(domain: str) -> None:
    slug = unique_slugs([domain])[domain]
    assert slug.lower() not in {"index", "master_sources_catalog"}


def test_a_domain_cannot_take_a_volume_file_name() -> None:
    slugs = unique_slugs(["A", "A Vol 2", "Design vol 10"])
    assert slugs["A"] == "A"
    assert slugs["A Vol 2"].lower() != "a_vol_2"
    assert not slugs["Design vol 10"].lower().endswith("_vol_10")


def test_yaml_values_are_escaped() -> None:
    assert yaml_str('He said "hi" \\ done') == '"He said \\"hi\\" \\\\ done"'
    s = Synthesizer(api_key="")
    text = s._frontmatter(
        'The "Best" Domain: Part 2', 1, 3, 'Chan "X"', "mock_notebook_0000", "offline-index"
    )
    assert 'domain: "The \\"Best\\" Domain: Part 2"' in text
    assert f'generator: "Notebook Alchemy v{__version__}"' in text


def test_notebook_link_only_for_real_ids() -> None:
    assert notebook_url(UUID).endswith(UUID)
    assert notebook_url("nb_hyperplexed") == ""


def test_offline_compendium_is_honest_and_links_real_files() -> None:
    comp = Synthesizer(api_key="").synthesize_cluster(
        "CSS Tricks",
        _videos(5),
        force_offline=True,
        notebook_title="Hyperplexed",
        notebook_id="nb_hyperplexed",
        slug="CSS_Tricks",
        related_domains=[("JS_Basics", "JS Basics")],
    )
    text = comp.content
    assert "Offline index" in text and "No transcripts were read" in text
    assert "notebooklm.google.com" not in text  # synthetic id gets no dead link
    assert SECTION_RELATED in text and "[[JS_Basics|JS Basics]]" in text
    for canned in (
        "Rule of Specificity",
        "Asynchronous Client Guardrails",
        "SOP-01",
        "Lead_Generation",
    ):
        assert canned not in text
    assert comp.relative_path == "compendiums/CSS_Tricks.md"
    assert not comp.is_live_synthesized
    assert text.count("### ") == 5


def test_volume_suffix_in_path() -> None:
    comp = Synthesizer(api_key="").synthesize_cluster("D", _videos(1), volume=3, force_offline=True)
    assert comp.relative_path == "compendiums/D_Vol_3.md"


def test_live_synthesis_covers_every_video_and_adds_the_source_table() -> None:
    fake = FakeGemini()
    synth = Synthesizer(api_key=KEY, gemini=fake, transcripts=FakeTranscripts(missing={"id004"}))
    comp = synth.synthesize_cluster("Big Domain", _videos(50), notebook_title="Chan", notebook_id=UUID)
    prompt = fake.prompts[0]
    assert all(f"### [{n}] Video {n - 1}\n" in prompt for n in range(1, 51))  # no 35-video cap
    assert comp.is_live_synthesized
    assert "## Sources" in comp.content and comp.content.count("| `id") == 50
    assert "none (NoTranscriptFound)" in comp.content
    assert any("had no transcript" in w for w in comp.warnings)
    assert f"notebooklm.google.com/notebook/{UUID}" in comp.content
    assert 'synthesis: "gemini:fake-model"' in comp.content


def test_long_transcripts_are_shortened_to_a_budget_and_reported() -> None:
    fake = FakeGemini()
    synth = Synthesizer(api_key=KEY, gemini=fake, transcripts=FakeTranscripts(text="w" * 200_000))
    comp = synth.synthesize_cluster("D", _videos(2))
    assert any("shortened" in w for w in comp.warnings)
    assert len(fake.prompts[0]) < 200_000  # two videos at 60,000 characters each, plus the instructions


def test_truncated_output_is_a_warning() -> None:
    fake = FakeGemini(GeminiResult("## Executive Summary\npartial", "MAX_TOKENS", model="fake-model"))
    comp = Synthesizer(api_key=KEY, gemini=fake, transcripts=FakeTranscripts()).synthesize_cluster(
        "D", _videos(3)
    )
    assert any("output limit" in w for w in comp.warnings)
    assert comp.content.rstrip().endswith("|")  # the deterministic source table still lands


def test_live_failure_falls_back_to_the_offline_index_with_a_warning() -> None:
    synth = Synthesizer(api_key=KEY, gemini=FakeGemini(GeminiError("quota")), transcripts=FakeTranscripts())
    comp = synth.synthesize_cluster("D", _videos(2))
    assert not comp.is_live_synthesized
    assert "Offline index" in comp.content
    assert any("quota" in w and "offline index" in w for w in comp.warnings)


def test_fenced_model_output_is_unwrapped() -> None:
    fake = FakeGemini(
        GeminiResult("```markdown\n## Executive Summary\nBody\n```", "STOP", model="fake-model")
    )
    comp = Synthesizer(api_key=KEY, gemini=fake, transcripts=FakeTranscripts()).synthesize_cluster(
        "D", _videos(1)
    )
    assert "```" not in comp.content


def test_force_offline_never_calls_gemini() -> None:
    fake = FakeGemini()
    Synthesizer(api_key=KEY, gemini=fake).synthesize_cluster("D", _videos(1), force_offline=True)
    assert fake.prompts == []


def test_index_and_catalog() -> None:
    synth = Synthesizer(api_key="")
    comps = [synth.synthesize_cluster("Dom A", _videos(2), force_offline=True)]
    index = synth.generate_index(comps, notebook_title='Chan "X"', total_sources=2)
    assert 'title: "Chan \\"X\\" - Master Compendium Index"' in index
    assert "offline index" in index and "Dom A" in index
    catalog = synth.generate_master_sources_catalog(
        [{"id": "a", "title": "P|Q", "url": "https://u", "topic": "Dom A"}], notebook_title="Chan"
    )
    assert "| 1 | `a` | P-Q | Dom A |" in catalog and "total_sources: 1" in catalog


def test_append_updates_count_and_keeps_related_section() -> None:
    synth = Synthesizer(api_key="")
    base = synth.synthesize_cluster(
        "Dom", _videos(2), force_offline=True, related_domains=[("Other", "Other")]
    ).content
    updated = synth.append_to_domain_compendium(
        base, [{"id": "n1", "title": "New", "url": "https://u"}], domain="Dom"
    )
    assert re.search(r"^source_count: 3$", updated, re.MULTILINE)
    assert updated.index("[Added] [New](https://u)") < updated.index(SECTION_RELATED)
    assert synth.append_to_domain_compendium(base, [], domain="Dom") == base


@pytest.mark.parametrize("key", ["", "your_gemini_api_key_here", "short"])
def test_placeholder_keys_are_not_live(key: str) -> None:
    fake = FakeGemini()
    comp = Synthesizer(api_key=key, gemini=fake).synthesize_cluster("D", _videos(1))
    assert not comp.is_live_synthesized and fake.prompts == []


def test_fallback_notice_becomes_a_warning_and_frontmatter_names_the_answering_model() -> None:
    fake = FakeGemini(GeminiResult("## Executive Summary\nBody", "STOP", model="gemini-3.5-flash-lite"))
    fake.notices = ["gemini-3.8-flash was unavailable (HTTP 503); used gemini-3.5-flash-lite."]
    comp = Synthesizer(api_key=KEY, gemini=fake, transcripts=FakeTranscripts()).synthesize_cluster(
        "D", _videos(2)
    )
    assert 'synthesis: "gemini:gemini-3.5-flash-lite"' in comp.content
    assert any("used gemini-3.5-flash-lite" in w for w in comp.warnings)


def test_notices_survive_a_failed_live_synthesis() -> None:
    fake = FakeGemini(GeminiError("both models down"))
    fake.notices = ["gemini-3.8-flash was unavailable (HTTP 503); used gemini-3.5-flash-lite."]
    comp = Synthesizer(api_key=KEY, gemini=fake, transcripts=FakeTranscripts()).synthesize_cluster(
        "D", _videos(1)
    )
    assert not comp.is_live_synthesized
    assert any("was unavailable" in w for w in comp.warnings) and any(
        "both models down" in w for w in comp.warnings
    )


def test_frontmatter_records_only_real_notebook_ids() -> None:
    synth = Synthesizer(api_key="")
    derived = synth.synthesize_cluster("D", _videos(1), force_offline=True, notebook_id="nb_hyperplexed")
    real = synth.synthesize_cluster("D", _videos(1), force_offline=True, notebook_id=UUID)
    sample = synth.synthesize_cluster("D", _videos(1), force_offline=True, notebook_id="mock_notebook_0000")
    assert "notebook_id:" not in derived.content and "sample_data" not in derived.content
    assert f'notebook_id: "{UUID}"' in real.content
    assert "sample_data: true" in sample.content and "notebook_id:" not in sample.content
