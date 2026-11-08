"""Passages, date rules, sorting, consolidation and the written books."""

from __future__ import annotations

import json
import re
from pathlib import Path

from notebook_alchemy.cleanup import Chunk
from notebook_alchemy.gemini import GeminiResult
from notebook_alchemy.knowledge import Segment, VideoLabels
from notebook_alchemy.library import (
    MISC,
    OTHER_POINTS,
    Library,
    LibraryBuilder,
    Passage,
    RequestCache,
    Section,
    Topic,
    apply_consolidation,
    apply_date_rules,
    build_passages,
)
from notebook_alchemy.writer import OVERVIEW_TITLE, SOURCE_WORD_LIMIT, render_library, render_topic

TODAY = "2026-11-08"


def _passage(pid: str, kind: str = "lasting", published: str = "2026-01-01", text: str = "words") -> Passage:
    vid = pid.split(":")[0]
    return Passage(pid, vid, f"Video {vid}", published, f"Point {pid}", kind, "", text)


# --- passages ---------------------------------------------------------------------------------------


def test_passages_keep_labelled_runs_and_never_lose_unlabelled_kept_chunks() -> None:
    chunks = [Chunk("v", n, f"chunk {n}") for n in range(1, 8)]
    chunks[2].duplicate_of = "w:1"  # said again in a newer video: not repeated here
    chunks[1].also_in = ["x"]
    labels = VideoLabels(
        "v",
        "k",
        "m",
        [
            Segment(1, 1, False),
            Segment(2, 4, True, "Fees", "figure", "about SAM.gov"),
            Segment(5, 5, True, "Steps", "rule", text="Chunk 5."),
        ],
    )
    passages = build_passages("v", "Video V", "2025-01-01", chunks, labels)
    assert [(p.headline, p.text) for p in passages] == [
        ("Fees", "chunk 2 chunk 4"),
        ("Steps", "Chunk 5."),  # the punctuated words, accepted because they are the same words
        (OTHER_POINTS, "chunk 6 chunk 7"),
    ]
    assert passages[0].also_in == ["x"] and passages[0].note == "about SAM.gov"


def test_punctuated_text_that_changed_the_words_is_not_used() -> None:
    chunks = [Chunk("v", 1, "the cage code takes ten days")]
    labels = VideoLabels(
        "v", "k", "m", [Segment(1, 1, True, "CAGE", text="The CAGE code takes about two weeks.")]
    )
    assert build_passages("v", "T", "", chunks, labels)[0].text == "the cage code takes ten days"


def test_a_video_with_no_labels_keeps_all_its_words() -> None:
    chunks = [Chunk("v", 1, "one"), Chunk("v", 2, "two")]
    assert [p.text for p in build_passages("v", "T", "", chunks, None)] == ["one two"]


# --- dates ------------------------------------------------------------------------------------------


def test_old_news_is_dropped_old_rules_are_marked_and_undated_ones_are_left_alone() -> None:
    passages = [
        _passage("a:1", "news", "2025-01-15"),  # 22 months: dropped
        _passage("b:1", "news", "2026-06-01"),  # recent news: kept
        _passage("c:1", "rule", "2024-03-01"),  # 32 months: kept, marked
        _passage("d:1", "figure", "2026-01-01"),  # recent figure: kept as is
        _passage("e:1", "news", ""),  # no date: never judged
        _passage("f:1", "lasting", "2019-01-01"),  # a principle does not age
    ]
    kept, removed = apply_date_rules(passages, TODAY)
    assert [p.id for p in removed] == ["a:1"]
    assert {p.id: p.as_of for p in kept} == {"b:1": "", "c:1": "2024", "d:1": "", "e:1": "", "f:1": ""}


# --- consolidation ----------------------------------------------------------------------------------


def test_consolidation_orders_removes_duplicates_and_superseded_and_loses_nothing_else() -> None:
    ps = [_passage(f"v{i}:1") for i in range(5)]
    answer = json.dumps(
        {"intro": "Start here.", "order": [3, 0, 99, 3], "duplicates": {"1": 0}, "superseded": [2]}
    )
    intro, ordered, dupes, superseded = apply_consolidation(answer, ps)
    assert intro == "Start here." and dupes == 1 and [p.id for p in superseded] == ["v2:1"]
    assert [p.id for p in ordered] == ["v3:1", "v0:1", "v4:1"]  # 4 was not mentioned: kept at the end
    assert ps[0].also_in == ["v1"]  # the kept passage cites the video it absorbed


def test_a_duplicate_loop_keeps_one_passage() -> None:
    ps = [_passage("a:1"), _passage("b:1")]
    _intro, ordered, _d, _s = apply_consolidation(json.dumps({"duplicates": {"0": 1, "1": 0}}), ps)
    assert len(ordered) >= 1


# --- the builder ------------------------------------------------------------------------------------


class SortingGemini:
    model = "gemini-test"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, **kw) -> GeminiResult:
        assert kw.get("allow_fallback") is False
        self.prompts.append(prompt)
        if "study library" in prompt:
            return GeminiResult(
                json.dumps(
                    {
                        "notebook_title": "GovCon Library",
                        "topics": [
                            {
                                "name": "Registration",
                                "sections": [{"name": "SAM.gov", "about": "signing up"}],
                            },
                            {"name": "Bidding", "sections": [{"name": "Pricing", "about": "prices"}]},
                        ],
                    }
                ),
                "STOP",
            )
        if "Place each numbered passage" in prompt:
            # passages of videos "p*" go to Pricing, "s*" to SAM.gov; "x*" is left unplaced
            out = {
                n: 1 if pid.startswith("p") else 0
                for n, pid in re.findall(r"^(\d+): Point (\w+):", prompt, re.MULTILINE)
                if not pid.startswith("x")
            }
            return GeminiResult(json.dumps({"assignments": out}), "STOP")
        nums = re.findall(r"^(\d+): \[", prompt, re.MULTILINE)
        return GeminiResult(json.dumps({"intro": "About this.", "order": [int(n) for n in nums]}), "STOP")


def test_the_builder_sorts_consolidates_and_reuses_every_answer(tmp_path: Path) -> None:
    passages = [_passage("s1:1"), _passage("s2:1"), _passage("p1:1"), _passage("p2:1"), _passage("x9:1")]
    gemini = SortingGemini()
    cache = RequestCache(tmp_path / "library.json")
    library = LibraryBuilder(gemini, cache, TODAY, "Kizzy Parks").build(passages, ["t1", "t2"])
    assert library.notebook_title == "GovCon Library"
    names = {t.name: [p.id for s in t.sections for p in s.passages] for t in library.topics}
    assert names["Registration"] == ["s1:1", "s2:1"] and names["Bidding"] == ["p1:1", "p2:1"]
    assert names[MISC] == ["x9:1"] and library.unplaced == 1
    asked = len(gemini.prompts)
    again = LibraryBuilder(gemini, RequestCache(tmp_path / "library.json"), TODAY, "Kizzy Parks").build(
        [_passage(p.id) for p in passages], ["t1", "t2"]
    )
    assert len(gemini.prompts) == asked and len(again.topics) == 3  # a rerun asks nothing


# --- the books --------------------------------------------------------------------------------------


def _library() -> Library:
    p = _passage("v1:1", "rule", "2024-02-02", text="Register first. Then request the CAGE code.")
    p.as_of, p.also_in, p.note = "2024", ["v2"], "SAM.gov is the federal registration site"
    return Library("GovCon", [Topic("Registration", [Section("SAM.gov", "signing up", "Start here.", [p])])])


def test_a_topic_book_is_plain_markdown_with_its_sources_named() -> None:
    books = render_library(
        _library(),
        "Kizzy Parks",
        {"v1": "Video one", "v2": "Video two", "v3": "Never read"},
        {"v1", "v2"},
        {"v1"},
        {"v1": "2024-02-02", "v2": "2025-01-01"},
    )
    assert [b.title for b in books] == [OVERVIEW_TITLE, "Registration"]
    book = books[1].content
    assert not book.startswith("---") and "## SAM.gov" in book and "### Point v1:1" in book
    assert "> Editor's note: SAM.gov is the federal registration site" in book
    assert '*From "Video v1" (Feb 2024)*; also said in "Video two". **As of 2024' in book
    overview = books[0].content
    assert "Videos read: 2 of 3" in overview and "  - Never read" in overview


def test_a_topic_longer_than_one_source_is_split_into_parts() -> None:
    big = [_passage(f"v{i}:1", text="word " * 60_000) for i in range(9)]
    topic = Topic("Huge", [Section("A", passages=big[:5]), Section("B", passages=big[5:])])
    books = render_topic(topic, "C", {}, set())
    assert [b.title for b in books][:2] == ["Huge", "Huge (Part 2)"]
    assert all(b.words <= SOURCE_WORD_LIMIT + 2000 for b in books)  # the heading and intro aside
