"""The no-key path: topics from the words that set video titles apart, no AI involved.

Without a Gemini key the books hold each video's cleaned, deduplicated words, grouped by title
words. They say plainly that no AI sorted or edited them.
"""

from __future__ import annotations

import re

from .library import Library, Passage, Section, Topic

_WORD_RE = re.compile(r"[a-z][a-z0-9.'-]{3,}", re.IGNORECASE)
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
        # Title filler: words that say how a video is pitched, not what it is about.
        "actually",
        "anyone",
        "anything",
        "everyone",
        "people",
        "watch",
        "stop",
        "live",
        "story",
        "still",
        "tell",
        "time",
        "years",
        "year",
        "month",
        "week",
        "days",
        "today",
        "become",
        "fast",
        "fastest",
        "easy",
        "secret",
        "secrets",
        "truth",
        "real",
        "reveals",
        "breaking",
        "ultimate",
        "free",
        "side",
        "dark",
        "means",
        "ends",
        "changes",
        "changed",
        "lost",
        "learned",
        "lessons",
        "mistake",
        "mistakes",
        "avoid",
        "help",
        "figure",
        "using",
        "working",
        "type",
        "poor",
        "rich",
        "experience",
        "understand",
        "minutes",
        "hours",
        "forever",
        "life",
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
    skipped. Titles no distinctive word claims are kept together as OTHER_VIDEOS.
    """
    ignore = {_stem(w) for w in _WORD_RE.findall(domain.lower())} | _STOP_WORDS
    # stem -> how often each spelling appears; the most common names the group, so one new video
    # does not rename a book (a renamed book is uploaded again as a new source).
    spellings: dict[str, dict[str, int]] = {}
    words: list[set[str]] = []
    for title in titles:
        stems = set()
        for original in _WORD_RE.findall(title):
            w = original.lower()
            s = _stem(w)
            if s not in ignore and w not in _STOP_WORDS:
                stems.add(s)
                seen = spellings.setdefault(s, {})
                seen[original] = seen.get(original, 0) + 1
        words.append(stems)
    shown = {s: max(c.items(), key=lambda kv: (kv[1], kv[0]))[0] for s, c in spellings.items()}
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
        name = ", ".join(_display(shown.get(w, w)) for w in ranked[:3])
        if name in groups:
            n = 2
            while f"{name} ({n})" in groups:
                n += 1
            name = f"{name} ({n})"
        groups[name] = members
        remaining -= set(members)
    if remaining:
        groups[OTHER_VIDEOS] = sorted(remaining)
    return groups


def _display(word: str) -> str:
    """A title word as the titles wrote it, capitalised if they did not ("sam.gov" -> "Sam.gov",
    "CPARS" stays, "here's" -> "Here's")."""
    word = word.strip(".-'")
    return word if any(c.isupper() for c in word) else word[:1].upper() + word[1:]


OTHER_VIDEOS = "Other videos"
# Videos in one section at most: a large group is split into several sections of the same name.
SECTION_VIDEOS_AT_MOST = 80
# Words per book: under SOURCE_WORD_LIMIT, so a book is one source and leaves room to grow.
BOOK_WORDS = 400_000
NO_AI_NOTE = "Sorted by the words in video titles; no AI selected, sorted or edited this text."


def library_without_ai(channel: str, passages: list[Passage]) -> Library:
    """A few books, each up to BOOK_WORDS words, of sections that group related video titles.

    Each section holds the videos whose titles share a distinctive word, and each video's words are
    one passage. Sections are packed into books in order (most common words first), so a channel
    of 3 million words becomes about eight sources rather than one per word group.
    """
    titles = [p.title for p in passages]
    sections: list[Section] = []
    for name, members in group_by_words(channel, titles, SECTION_VIDEOS_AT_MOST).items():
        section = Section(name, about=NO_AI_NOTE, intro=NO_AI_NOTE)
        section.passages = [passages[i] for i in members]
        sections.append(section)
    books: list[list[Section]] = []
    words = 0
    for section in sections:
        size = sum(p.words for p in section.passages)
        if not books or (words + size > BOOK_WORDS and books[-1]):
            books.append([])
            words = 0
        books[-1].append(section)
        words += size
    topics: list[Topic] = []
    used: set[str] = set()
    for book in books:
        named = [s.name.split(", ")[0] for s in book if s.name != OTHER_VIDEOS][:3]
        name = "Videos on " + ", ".join(named) if named else OTHER_VIDEOS
        base, n = name, 2
        while name in used:
            name, n = f"{base} ({n})", n + 1
        used.add(name)
        topics.append(Topic(name, book))
    return Library(channel, topics, sorted_by_ai=False)
