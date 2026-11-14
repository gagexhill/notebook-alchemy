"""The no-key path: a few books of title-word sections, no AI involved."""

from __future__ import annotations

from notebook_alchemy.library import Passage
from notebook_alchemy.offline import BOOK_WORDS, OTHER_VIDEOS, group_by_words, library_without_ai


def _video(i: int, title: str, words: int) -> Passage:
    return Passage(f"v{i}:1", f"v{i}", title, "2026-01-01", title, "lasting", "", "word " * words)


def test_title_words_keep_the_spelling_the_titles_use() -> None:
    titles = ["CPARS ratings explained", "Fix your CPARS score", "here's sam.gov help", "sam.gov here's how"]
    assert list(group_by_words("Chan", titles, 80)) == ["Sam.gov, Here's", "CPARS"]  # not "Here'S", "Cpars"


def test_a_large_channel_becomes_a_few_books_not_one_per_word() -> None:
    topics = ["contracts", "proposals", "pricing", "teaming", "grants", "audits", "bonds", "leases"]
    passages = [_video(i, f"{topics[i % 8]} talk {i}", 20_000) for i in range(160)]  # 3.2M words
    library = library_without_ai("Chan", passages)
    assert 8 <= len(library.topics) <= 10  # about total / BOOK_WORDS
    assert all(sum(p.words for s in t.sections for p in s.passages) <= BOOK_WORDS for t in library.topics)
    assert sum(len(s.passages) for t in library.topics for s in t.sections) == 160  # every video kept
    assert len({t.name for t in library.topics}) == len(library.topics)
    assert all(t.name.startswith("Videos on ") for t in library.topics)


def test_a_small_channel_is_one_book_with_sections() -> None:
    titles = ["Pricing basics", "Pricing math", "Teaming rules", "Teaming deals", "Odd one", "Other one"]
    library = library_without_ai("Chan", [_video(i, t, 500) for i, t in enumerate(titles)])
    assert [t.name for t in library.topics] == ["Videos on Teaming, Pricing"]  # in section order
    assert [s.name for s in library.topics[0].sections] == ["Teaming", "Pricing", OTHER_VIDEOS]


def test_a_new_video_does_not_rename_a_group() -> None:
    titles = ["Beginner course", "Beginner basics", "Pricing math", "Pricing talk", "Teaming deals", "Odd"]
    before = set(group_by_words("Chan", titles, 80))
    after = set(group_by_words("Chan", ["Beginners start here", *titles], 80))
    assert "Beginner, Basics" in before or any(n.startswith("Beginner") for n in before)
    beginner_after = [n for n in after if n.lower().startswith("beginner")]
    assert beginner_after and beginner_after[0].split(",")[0] == "Beginner"  # the common spelling wins
