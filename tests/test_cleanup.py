"""Local cleanup: noise and filler, chunking, boilerplate and cross-video duplicates."""

from __future__ import annotations

from notebook_alchemy.cleanup import (
    CHUNK_MAX_WORDS,
    Chunk,
    chunk_video,
    clean_text,
    is_punctuated,
    mark_duplicates,
    split_chunks,
)

FACT = (
    "To register in SAM.gov you need your EIN exactly as it appears on the IRS letter, a physical "
    "address, and a bank account for payments, and the UEI usually arrives within ten business days"
)


def test_noise_filler_and_stutters_go_but_words_and_numbers_stay() -> None:
    raw = "[Music] um so the the CAGE code uh takes 10 days >> and costs $0 [Applause]"
    assert clean_text(raw) == "so the CAGE code takes 10 days and costs $0"


def test_punctuated_text_is_split_by_sentence_and_unpunctuated_by_window() -> None:
    sentences = " ".join(f"Sentence number {i} has a few words in it." for i in range(30))
    chunks = split_chunks(sentences)
    assert all(c.endswith(".") for c in chunks) and all(len(c.split()) <= CHUNK_MAX_WORDS for c in chunks)
    flat = " ".join(f"word{i}" for i in range(200))
    assert not is_punctuated(flat)
    windows = split_chunks(flat)
    assert len(windows) == 5 and " ".join(windows).split() == flat.split()  # nothing lost


def test_a_short_call_to_action_is_marked_but_content_mentioning_it_is_not() -> None:
    text = (
        "Make sure to subscribe and hit the bell. "
        + FACT
        + ". If you subscribe to the SAM.gov alerts for your NAICS code you get notices the day they post, "
        "which gives you a week more than bidders who search by hand each Friday afternoon."
    )
    chunks = chunk_video("v1", text)
    assert chunks[0].boilerplate and "subscribe" in chunks[0].text
    assert not any(c.boilerplate for c in chunks[1:])  # a long passage that says "subscribe" is content


def test_a_passage_repeated_across_videos_is_kept_once_in_the_newest_video() -> None:
    old = [
        Chunk("old", 1, FACT),
        Chunk("old", 2, "Older only fact about 8a certification timelines and fees."),
    ]
    new = [Chunk("new", 1, FACT + " today")]
    assert mark_duplicates([("2023-01-01", old), ("2025-06-01", new)]) == 1
    assert old[0].duplicate_of == "new:1" and new[0].kept and new[0].also_in == ["old"]
    assert old[1].kept  # what only the older video says stays


def test_a_passage_repeated_inside_one_video_is_kept_once() -> None:
    chunks = [
        Chunk("v", 1, FACT),
        Chunk("v", 2, "Something else entirely about pricing."),
        Chunk("v", 3, FACT),
    ]
    mark_duplicates([("2024-01-01", chunks)])
    assert [c.kept for c in chunks] == [True, True, False] and chunks[2].duplicate_of == "v:1"


def test_undated_videos_are_compared_last() -> None:
    dated, undated = [Chunk("dated", 1, FACT)], [Chunk("undated", 1, FACT)]
    mark_duplicates([("", undated), ("2022-05-05", dated)])
    assert dated[0].kept and undated[0].duplicate_of == "dated:1"
