"""Passages, date rules, sorting, consolidation and the written books."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import notebook_alchemy.library as library_module
from notebook_alchemy.cleanup import Chunk
from notebook_alchemy.errors import GeminiError
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
    merge_map,
    parse_extension,
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
    ps[2].kind, ps[2].published = "rule", "2024-01-01"  # an older rule that the others, newer, replace
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
    """Places "p*" videos in Pricing and "s*" in SAM.gov; "x*" fit nowhere until the map is extended."""

    model = "gemini-test"

    def __init__(
        self, new_section: str = "Extras", places_x: bool = True, subsections: list[str] | None = None
    ) -> None:
        self.prompts: list[str] = []
        self.new_section, self.places_x = new_section, places_x
        self.subsections = ["Alpha", "Beta"] if subsections is None else subsections
        self.quota_on: str | None = None  # a prompt phrase that answers with a used-up quota

    def asked(self, phrase: str) -> list[str]:
        return [p for p in self.prompts if phrase in p]

    def generate(self, prompt: str, **kw) -> GeminiResult:
        assert kw.get("allow_fallback") is False
        self.prompts.append(prompt)
        if "fit none of them" in prompt:
            added = {"name": "Bidding", "sections": [{"name": self.new_section, "about": "the rest"}]}
            return GeminiResult(json.dumps({"topics": [added]}), "STOP")
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
        if self.quota_on and self.quota_on in prompt:
            raise GeminiError("quota", status=429, quota=True)
        if "has grown to" in prompt:
            return GeminiResult(json.dumps({"subsections": self.subsections}), "STOP")
        if "Place each numbered passage" in prompt:
            points = re.findall(r"^(\d+): Point (\w+):", prompt, re.MULTILINE)
            if "0: Alpha" in prompt:  # subsections: odd-numbered videos go to Beta
                out = {n: int(pid[1:]) % 2 for n, pid in points}
                return GeminiResult(json.dumps({"assignments": out}), "STOP")
            sections = re.findall(r"^(\d+): \w+ / (.+?) —", prompt, re.MULTILINE)
            index = {name: int(i) for i, name in sections}
            out = {}
            for n, pid in points:
                if pid.startswith("x"):
                    if self.places_x and "Extras" in index:
                        out[n] = index["Extras"]
                else:
                    out[n] = index["Pricing"] if pid.startswith("p") else index["SAM.gov"]
            return GeminiResult(json.dumps({"assignments": out}), "STOP")
        nums = re.findall(r"^(\d+): \[", prompt, re.MULTILINE)
        return GeminiResult(json.dumps({"intro": "About this.", "order": [int(n) for n in nums]}), "STOP")


def test_the_builder_sorts_consolidates_and_reuses_every_answer(tmp_path: Path) -> None:
    passages = [_passage("s1:1"), _passage("s2:1"), _passage("p1:1"), _passage("p2:1"), _passage("x9:1")]
    gemini = SortingGemini()
    cache = RequestCache(tmp_path / "library.json")
    library = LibraryBuilder(gemini, cache, TODAY, "Example Channel").build(passages, ["t1", "t2"])
    assert library.notebook_title == "GovCon Library"
    names = {t.name: [p.id for s in t.sections for p in s.passages] for t in library.topics}
    assert names["Registration"] == ["s1:1", "s2:1"] and names["Bidding"] == ["p1:1", "p2:1"]
    assert names[MISC] == ["x9:1"] and library.unplaced == 1
    asked = len(gemini.prompts)
    again = LibraryBuilder(gemini, RequestCache(tmp_path / "library.json"), TODAY, "Example Channel").build(
        [_passage(p.id) for p in passages], ["t1", "t2"]
    )
    assert len(gemini.prompts) == asked and len(again.topics) == 3  # a rerun asks nothing


def _build(tmp_path: Path, gemini: SortingGemini, ids: list[str]) -> Library:
    cache = RequestCache(tmp_path / "library.json")
    return LibraryBuilder(gemini, cache, TODAY, "Example Channel").build([_passage(i) for i in ids], ["t1"])


def _ids(library: Library) -> dict[str, list[str]]:
    return {f"{t.name}/{s.name}": [p.id for p in s.passages] for t in library.topics for s in t.sections}


XS = [f"x{i}:1" for i in range(1, 7)]  # more unplaced passages than EXTEND_AT_LEAST


def test_passages_that_fit_nowhere_extend_the_map_and_nothing_else_moves(tmp_path: Path) -> None:
    gemini = SortingGemini()
    library = _build(tmp_path, gemini, ["s1:1", "p1:1", *XS])
    assert _ids(library) == {
        "Registration/SAM.gov": ["s1:1"],
        "Bidding/Pricing": ["p1:1"],
        "Bidding/Extras": XS,
    }
    assert len(gemini.asked("fit none of them")) == 1 and len(gemini.asked("You are organising")) == 1
    replaced = gemini.asked("Place each numbered passage")[-1]
    assert "Point s1:1" not in replaced and "Point x1:1" in replaced  # only the unplaced are asked again
    asked = len(gemini.prompts)
    _build(tmp_path, gemini, ["s1:1", "p1:1", *XS])
    assert len(gemini.prompts) == asked  # a rerun asks nothing


def test_passages_that_still_fit_nowhere_do_not_extend_the_map_again(tmp_path: Path) -> None:
    gemini = SortingGemini(new_section="Unrelated", places_x=False)
    library = _build(tmp_path, gemini, ["s1:1", "p1:1", *XS])
    assert _ids(library)[f"{MISC}/{MISC}"] == XS
    asked = len(gemini.prompts)
    _build(tmp_path, gemini, ["s1:1", "p1:1", *XS])
    assert len(gemini.prompts) == asked and len(gemini.asked("fit none of them")) == 1


def test_a_new_video_is_placed_without_moving_or_re_sorting_the_rest(tmp_path: Path) -> None:
    gemini = SortingGemini()
    _build(tmp_path, gemini, ["s1:1", "p1:1"])
    before = len(gemini.prompts)
    library = _build(tmp_path, gemini, ["s1:1", "p1:1", "s2:1"])
    new = gemini.prompts[before:]
    assert not any("study library" in p for p in new)  # the map is kept
    placed = [p for p in new if "Place each numbered passage" in p]
    assert len(placed) == 1 and "Point s2:1" in placed[0] and "Point s1:1" not in placed[0]
    ordered = [p for p in new if "Place each" not in p]
    assert len(ordered) == 1 and "Pricing" not in ordered[0]  # only the section that changed
    assert _ids(library)["Registration/SAM.gov"] == ["s1:1", "s2:1"]


def test_resort_rebuilds_the_map(tmp_path: Path) -> None:
    gemini = SortingGemini()
    _build(tmp_path, gemini, ["s1:1", "p1:1"])
    cache = RequestCache(tmp_path / "library.json")
    LibraryBuilder(gemini, cache, TODAY, "Example Channel").build([_passage("s1:1")], ["t1"], resort=True)
    assert len(gemini.asked("You are organising")) == 2


def test_a_large_section_is_divided_into_subsections_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(library_module, "SUBSECTION_AT", 3)
    gemini = SortingGemini()
    library = _build(tmp_path, gemini, [f"s{i}:1" for i in range(1, 6)])
    assert _ids(library) == {
        "Registration/SAM.gov: Alpha": ["s2:1", "s4:1"],
        "Registration/SAM.gov: Beta": ["s1:1", "s3:1", "s5:1"],
    }
    before = len(gemini.prompts)
    library = _build(tmp_path, gemini, [f"s{i}:1" for i in range(1, 7)])
    new = gemini.prompts[before:]
    assert not any("has grown to" in p for p in new)  # the names are kept
    assert _ids(library)["Registration/SAM.gov: Alpha"] == ["s2:1", "s4:1", "s6:1"]
    assert sum("0: Alpha" in p for p in new) == 1 and "Point s1:1" not in next(
        p for p in new if "0: Alpha" in p
    )


def test_extending_the_map_keeps_every_existing_topic_and_section() -> None:
    topics = [
        Topic("Bidding", [Section("Pricing", about="prices")]),
        Topic("Registration", [Section("SAM.gov")]),
    ]
    additions = parse_extension(
        json.dumps(
            {
                "topics": [
                    {"name": "bidding", "sections": [{"name": "pricing", "about": "x"}, {"name": "Teaming"}]},
                    {"name": "Hiring", "sections": [{"name": "Recruiters"}]},
                    {"name": MISC, "sections": [{"name": "Junk"}]},
                ]
            }
        )
    )
    merged = merge_map(topics, additions)
    assert [(t.name, [s.name for s in t.sections]) for t in merged] == [
        ("Bidding", ["Pricing", "Teaming"]),
        ("Registration", ["SAM.gov"]),
        ("Hiring", ["Recruiters"]),
    ]
    assert merged[0].sections[0].about == "prices"


# --- the books --------------------------------------------------------------------------------------


def _library() -> Library:
    p = _passage("v1:1", "rule", "2024-02-02", text="Register first. Then request the CAGE code.")
    p.as_of, p.also_in, p.note = "2024", ["v2"], "SAM.gov is the federal registration site"
    return Library("GovCon", [Topic("Registration", [Section("SAM.gov", "signing up", "Start here.", [p])])])


def test_a_topic_book_is_plain_markdown_with_its_sources_named() -> None:
    books = render_library(
        _library(),
        "Example Channel",
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


def _sized(name: str, words: int) -> Section:
    return Section(
        name,
        passages=[_passage(f"{name.lower()}{i}:1", text="word " * 50_000) for i in range(words // 50_000)],
    )


def test_a_section_stays_in_its_part_and_a_new_section_goes_to_the_last_part() -> None:
    parts: dict[str, int] = {}
    first = render_topic(
        Topic("T", [_sized("A", 200_000), _sized("B", 200_000), _sized("C", 100_000)]), "C", {}, set(), parts
    )
    assert parts == {"A": 1, "B": 1, "C": 2} and len(first) == 2
    # D sits between A and B in the topic's order, but a new section never pushes into an earlier part.
    again = render_topic(
        Topic("T", [_sized("A", 200_000), _sized("D", 50_000), _sized("B", 200_000), _sized("C", 100_000)]),
        "C",
        {},
        set(),
        parts,
    )
    assert again[0].content == first[0].content  # Part 1 is unchanged, so it is not uploaded again
    assert parts == {"A": 1, "B": 1, "D": 2, "C": 2}
    assert again[1].content.index("## D") < again[1].content.index("## C")  # topic order within a part


def test_a_part_that_grows_past_the_limit_moves_only_its_last_section_on() -> None:
    parts = {"A": 1, "B": 1, "C": 2}
    books = render_topic(
        Topic("T", [_sized("A", 300_000), _sized("B", 200_000), _sized("C", 100_000)]), "C", {}, set(), parts
    )
    assert parts == {"A": 1, "B": 2, "C": 2} and [b.title for b in books] == ["T", "T (Part 2)"]
    assert all(b.words <= SOURCE_WORD_LIMIT + 2000 for b in books)


def test_parts_are_numbered_without_gaps_when_one_empties() -> None:
    parts = {"A": 1, "B": 2, "C": 3}
    books = render_topic(Topic("T", [_sized("A", 100_000), _sized("C", 100_000)]), "C", {}, set(), parts)
    assert [b.title for b in books] == ["T", "T (Part 2)"] and parts == {"A": 1, "C": 2}


def test_punctuation_is_copied_onto_the_speakers_words_and_nothing_else_changes() -> None:
    raw = "it comes to partnership this is important so for example with this company we partnered"
    model = (
        "When it comes to partnership, this is important. So, for example, with this company, we partnered."
    )
    labels = VideoLabels("v", "k", "m", [Segment(1, 1, True, "Partners", text=model)])
    text = build_passages("v", "T", "", [Chunk("v", 1, raw)], labels)[0].text
    assert (
        text
        == "it comes to partnership, this is important. So, for example, with this company, we partnered."
    )
    assert [w.strip(",.").lower() for w in text.split()] == raw.split()  # every word, in order, none added


def test_a_word_the_model_dropped_stays() -> None:
    raw = "the cage code takes ten business days to arrive after you register"
    dropped = "The CAGE code takes ten days to arrive after you register."  # "business" left out
    labels = VideoLabels("v", "k", "m", [Segment(1, 1, True, "C", text=dropped)])
    text = build_passages("v", "T", "", [Chunk("v", 1, raw)], labels)[0].text
    assert "business" in text and text.startswith("The CAGE code") and text.endswith("register.")


def test_superseded_only_removes_an_older_time_sensitive_passage() -> None:
    lasting = [_passage("a:1", "lasting", "2024-01-01"), _passage("b:1", "rule", "2026-01-01")]
    _i, ordered, _d, removed = apply_consolidation(json.dumps({"superseded": [0]}), lasting)
    assert removed == [] and len(ordered) == 2  # a principle is never "replaced"
    newest = [_passage("a:1", "rule", "2026-05-01"), _passage("b:1", "rule", "2024-01-01")]
    _i, ordered, _d, removed = apply_consolidation(json.dumps({"superseded": [0]}), newest)
    assert removed == []  # nothing newer stays, so the newest rule is kept
    _i, ordered, _d, removed = apply_consolidation(json.dumps({"superseded": [1]}), newest)
    assert [p.id for p in removed] == ["b:1"]


def test_a_chunk_the_model_never_saw_is_kept_even_inside_a_dropped_segment() -> None:
    chunks = [Chunk("v", n, f"chunk {n}") for n in range(1, 6)]
    labels = VideoLabels("v", "k", "m", [Segment(1, 5, False)], seen=[1, 2, 4, 5])  # 3 was hidden then
    passages = build_passages("v", "T", "", chunks, labels)
    assert [p.text for p in passages] == ["chunk 3"]


def test_the_library_cache_is_saved_as_each_answer_arrives(tmp_path: Path) -> None:
    cache = RequestCache(tmp_path / "lib.json")
    cache.put("k", "v")
    assert RequestCache(tmp_path / "lib.json").get("k") == "v"


def test_an_extension_that_adds_nothing_asks_nothing_more(tmp_path: Path) -> None:
    gemini = SortingGemini(new_section="Pricing")  # names only a section that already exists
    builder = LibraryBuilder(gemini, RequestCache(tmp_path / "library.json"), TODAY, "Example Channel")
    builder.build([_passage(i) for i in ["s1:1", "p1:1", *XS]], ["t1"])
    assert len(gemini.asked("Place each numbered passage")) == 1
    assert any("No new topics" in n for n in builder.notices)


# --- review findings: answers paid for once ---------------------------------------------------------


def test_an_unusable_extension_answer_is_not_paid_for_again(tmp_path: Path) -> None:
    gemini = SortingGemini(new_section="")  # an answer with no usable section
    _build(tmp_path, gemini, ["s1:1", "p1:1", *XS])
    _build(tmp_path, gemini, ["s1:1", "p1:1", *XS])
    assert len(gemini.asked("fit none of them")) == 1


def test_an_unusable_subsection_answer_is_not_paid_for_again(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(library_module, "SUBSECTION_AT", 3)
    gemini = SortingGemini(subsections=["Only one"])
    ids = [f"s{i}:1" for i in range(1, 6)]
    _build(tmp_path, gemini, ids)
    _build(tmp_path, gemini, ids)
    assert len(gemini.asked("has grown to")) == 1
    _build(tmp_path, gemini, [*ids, "s6:1"])  # the section changed: asked again
    assert len(gemini.asked("has grown to")) == 2


def test_subsection_names_survive_a_quota_stop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(library_module, "SUBSECTION_AT", 3)
    gemini = SortingGemini()
    gemini.quota_on = "0: Alpha"
    ids = [f"s{i}:1" for i in range(1, 6)]
    with pytest.raises(GeminiError):
        _build(tmp_path, gemini, ids)
    gemini.quota_on = None
    library = _build(tmp_path, gemini, ids)
    assert len(gemini.asked("has grown to")) == 1 and "Registration/SAM.gov: Alpha" in _ids(library)


def test_resort_forgets_subsections(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(library_module, "SUBSECTION_AT", 3)
    gemini = SortingGemini()
    ids = [f"s{i}:1" for i in range(1, 6)]
    _build(tmp_path, gemini, ids)
    cache = RequestCache(tmp_path / "library.json")
    LibraryBuilder(gemini, cache, TODAY, "Example Channel").build([_passage(i) for i in ids], ["t1"], resort=True)
    assert len(gemini.asked("has grown to")) == 2


def test_a_passage_placed_nowhere_is_asked_about_again_with_new_videos(tmp_path: Path) -> None:
    gemini = SortingGemini()
    first = _build(tmp_path, gemini, ["s1:1", "x9:1"])  # one unplaced: too few to extend the map
    assert _ids(first)[f"{MISC}/{MISC}"] == ["x9:1"]
    _build(tmp_path, gemini, ["s1:1", "x9:1"])
    assert len(gemini.asked("Place each numbered passage")) == 1  # nothing new: nothing asked
    _build(tmp_path, gemini, ["s1:1", "x9:1", "s2:1"])
    again = gemini.asked("Place each numbered passage")[-1]
    assert "Point s2:1" in again and "Point x9:1" in again and "Point s1:1" not in again
