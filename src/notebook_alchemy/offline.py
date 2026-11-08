"""The no-key path: topics from the words that set video titles apart, no AI involved.

Without a Gemini key the books hold each video's cleaned, deduplicated words, grouped by title
words. They say plainly that no AI sorted or edited them.
"""

from __future__ import annotations

import re

from .library import Library, Passage, Section, Topic

_WORD_RE = re.compile(r"[a-z][a-z0-9.'-]{3,}")
# Words too common in video titles to name a group.
_STOP_WORDS = frozenset(
    [
        "this",
        "that",
        "with",
        "from",
        "your",
        "what",
        "when",
        "where",
        "which",
        "their",
        "they",
        "them",
        "have",
        "will",
        "into",
        "about",
        "than",
        "then",
        "just",
        "more",
        "most",
        "only",
        "over",
        "also",
        "some",
        "here",
        "there",
        "these",
        "those",
        "does",
        "doing",
        "done",
        "make",
        "made",
        "making",
        "how",
        "why",
        "who",
        "video",
        "videos",
        "part",
        "full",
        "guide",
        "tips",
        "step",
        "steps",
        "best",
        "ways",
        "need",
        "know",
        "want",
        "first",
        "next",
        "every",
        "should",
        "would",
        "could",
        "really",
        "thing",
        "things",
        "before",
        "after",
        "without",
        "inside",
        "start",
        "started",
        "get",
        "getting",
        "you're",
        "it's",
        "don't",
        "i'm",
        "never",
        "ever",
    ]
)


def _stem(word: str) -> str:
    """A rough stem, so "contract", "contracts" and "contracting" count as one word."""
    for suffix in ("ings", "ing", "ers", "ies", "ed", "er", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def group_by_words(domain: str, titles: list[str], limit: int) -> dict[str, list[int]]:
    """Group titles by the words that set them apart, most common first; name each by its words.

    Words shared by most of the domain (and the domain's own name) say nothing, so they are
    skipped. Titles no distinctive word claims are kept together as "More".
    """
    ignore = {_stem(w) for w in _WORD_RE.findall(domain.lower())} | _STOP_WORDS
    shown: dict[str, str] = {}  # stem -> the first spelling seen, for the name
    words: list[set[str]] = []
    for title in titles:
        stems = set()
        for w in _WORD_RE.findall(title.lower()):
            s = _stem(w)
            if s not in ignore and w not in _STOP_WORDS:
                stems.add(s)
                shown.setdefault(s, w)
        words.append(stems)
    counts: dict[str, int] = {}
    for ws in words:
        for w in ws:
            counts[w] = counts.get(w, 0) + 1
    # A word in most of the domain's titles describes the domain, not a part of it.
    common = {w for w, n in counts.items() if n > len(titles) * 0.5}
    remaining = set(range(len(titles)))
    groups: dict[str, list[int]] = {}
    while remaining:
        tally: dict[str, int] = {}
        for i in remaining:
            for w in words[i] - common:
                tally[w] = tally.get(w, 0) + 1
        best = max(tally.items(), key=lambda kv: (kv[1], kv[0]), default=None)
        if best is None or best[1] < 2:
            break
        members = sorted(i for i in remaining if best[0] in words[i])[:limit]
        # Name the group by its most frequent distinctive words, the chosen word first.
        freq: dict[str, int] = {}
        for i in members:
            for w in words[i] - common:
                freq[w] = freq.get(w, 0) + 1
        ranked = [best[0]] + [
            w for w, n in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])) if w != best[0] and n >= 2
        ]
        name = ", ".join(shown.get(w, w).strip(".-'").title() for w in ranked[:3])
        if name in groups:
            n = 2
            while f"{name} ({n})" in groups:
                n += 1
            name = f"{name} ({n})"
        groups[name] = members
        remaining -= set(members)
    if remaining:
        groups["More"] = sorted(remaining)
    return groups


TOPICS_AT_MOST = 12
NO_AI_NOTE = "Sorted by the words in video titles; no AI selected, sorted or edited this text."


def library_without_ai(channel: str, passages: list[Passage]) -> Library:
    """One topic per group of related video titles; each video's words are one passage."""
    titles = [p.title for p in passages]
    size = max(1, -(-len(passages) // TOPICS_AT_MOST))
    topics = []
    for name, members in group_by_words(channel, titles, size).items():
        section = Section("From the videos", about=NO_AI_NOTE, intro=NO_AI_NOTE)
        section.passages = [passages[i] for i in members]
        topics.append(Topic(name, [section]))
    return Library(channel, topics)
