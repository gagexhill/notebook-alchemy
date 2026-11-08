"""The library as NotebookLM sources: one study book per topic, plus an overview.

Plain Markdown with headings NotebookLM can show and cite; no YAML frontmatter, which NotebookLM
would display as text. Each passage ends with the video it comes from, so a citation lands on a
readable source. A topic book is split into parts only past SOURCE_WORD_LIMIT words, below
NotebookLM's limit of 500,000 words per source.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .library import MISC, Library, Passage, Section, Topic

SOURCE_WORD_LIMIT = 450_000
OVERVIEW_TITLE = "_OVERVIEW"


@dataclass
class Book:
    """One NotebookLM source."""

    title: str
    content: str

    @property
    def words(self) -> int:
        return len(self.content.split())


def date_label(published: str, exact: bool) -> str:
    """ "Mar 2024" for an exact date, "2024" for an approximate one, "" when unknown."""
    try:
        day = dt.date.fromisoformat(published)
    except ValueError:
        return ""
    return day.strftime("%b %Y") if exact else str(day.year)


def _source_line(p: Passage, titles: dict[str, str], exact: set[str]) -> str:
    when = date_label(p.published, p.video_id in exact)
    line = f'*From "{p.title}"' + (f" ({when})" if when else "") + "*"
    others = [titles[v] for v in p.also_in if v in titles]
    if others:
        line += "; also said in " + ", ".join(f'"{t}"' for t in others[:5])
        if len(others) > 5:
            line += f" and {len(others) - 5} more"
    if p.as_of:
        line += f". **As of {p.as_of}: check the current rules before relying on this.**"
    return line


def _passage(p: Passage, titles: dict[str, str], exact: set[str]) -> str:
    parts = [f"### {p.headline}\n"]
    if p.note:
        parts.append(f"> Editor's note: {p.note}\n")
    parts.append(f"{p.text}\n")
    parts.append(_source_line(p, titles, exact) + "\n")
    return "\n".join(parts)


def _section(s: Section, titles: dict[str, str], exact: set[str]) -> str:
    head = f"## {s.name}\n\n" + (f"{s.intro}\n\n" if s.intro else "")
    return head + "\n".join(_passage(p, titles, exact) for p in s.passages)


def _intro(channel: str) -> str:
    return (
        f"A study book from {channel}'s videos, in the speakers' own words. Each passage names the video "
        'it comes from. Passages marked "As of" describe rules or figures that may have changed.\n'
    )


def render_topic(
    topic: Topic,
    channel: str,
    titles: dict[str, str],
    exact: set[str],
    parts: dict[str, int] | None = None,
) -> list[Book]:
    """The topic as one book, or as parts when it is longer than SOURCE_WORD_LIMIT words.

    `parts` (section name -> part number) is read and updated in place, so a section stays in the
    part it was in last time: new sections go to the last part, and only a part that grows past the
    limit moves its last sections on to the next part. Sections keep the topic's order within a part.
    """
    parts = {} if parts is None else parts
    blocks: list[tuple[str, str]] = []  # (key, text); a section cut into pieces gives one key per piece
    for s in topic.sections:
        text = _section(s, titles, exact)
        if len(text.split()) <= SOURCE_WORD_LIMIT:
            blocks.append((s.name, text))
            continue
        # A single section too large for one source is cut between passages.
        current, piece_no = f"## {s.name}\n\n" + (f"{s.intro}\n\n" if s.intro else ""), 1
        for p in s.passages:
            piece = _passage(p, titles, exact) + "\n"
            if len((current + piece).split()) > SOURCE_WORD_LIMIT:
                blocks.append((f"{s.name} #{piece_no}", current))
                current, piece_no = f"## {s.name} (continued)\n\n", piece_no + 1
            current += piece
        blocks.append((f"{s.name} #{piece_no}", current))

    order = {key: i for i, (key, _) in enumerate(blocks)}
    words = {key: len(text.split()) for key, text in blocks}
    last = max((parts[k] for k, _ in blocks if k in parts), default=1)
    placed: dict[int, list[str]] = {}
    for key, _ in blocks:
        placed.setdefault(parts.get(key, last), []).append(key)
    number = 1
    while number <= max(placed):
        keys = sorted(placed.get(number, []), key=order.__getitem__)
        while len(keys) > 1 and sum(words[k] for k in keys) > SOURCE_WORD_LIMIT:
            moved = keys.pop()  # the last section moves on; everything before it stays put
            placed.setdefault(number + 1, []).append(moved)
        placed[number] = keys
        number += 1

    texts = dict(blocks)
    parts.clear()
    books: list[Book] = []
    for keys in (placed[n] for n in sorted(placed) if placed[n]):
        n = len(books) + 1  # numbered without gaps, should a part empty out
        title = topic.name if n == 1 else f"{topic.name} (Part {n})"
        body = "\n".join(texts[k] for k in sorted(keys, key=order.__getitem__))
        books.append(Book(title, f"# {title}\n\n{_intro(channel)}\n{body}"))
        parts.update(dict.fromkeys(keys, n))
    return books


def render_overview(
    library: Library,
    channel: str,
    videos: dict[str, str],
    read: set[str],
    titles: dict[str, str],
    exact: set[str],
    dates: dict[str, str],
) -> Book:
    """What the notebook holds, how it was made, what is missing and what was left out."""
    lines = [
        f"# {library.notebook_title}: Overview\n",
        f"This notebook was built from the YouTube channel {channel} by Notebook Alchemy. Every video's "
        "words were read, repeated passages were kept once, and each point was filed under a topic and "
        "section. The topic books hold the speakers' own words; each passage names its video.\n",
        "## Topics\n",
        "| Topic | Sections | Passages | Words |",
        "| :-- | :-: | :-: | :-: |",
    ]
    for t in library.topics:
        passages = sum(len(s.passages) for s in t.sections)
        lines.append(f"| {t.name} | {len(t.sections)} | {passages} | {t.words:,} |")
    lines.append("")
    for t in library.topics:
        lines.append(f"### {t.name}\n")
        lines += [f"- {s.name}" + (f": {s.about}" if s.about else "") for s in t.sections]
        lines.append("")
    known = sorted(d for v, d in dates.items() if d and v in read)
    lines.append("## Coverage\n")
    lines.append(
        f"- Videos read: {len(read)} of {len(videos)}"
        + (f", published {date_label(known[0], False)} to {date_label(known[-1], False)}" if known else "")
    )
    lines.append(f"- Repeated passages kept once: {library.removed_duplicates:,}")
    missing = [v for v in videos if v not in read]
    if missing:
        lines.append(
            f"- Not read yet ({len(missing)}): YouTube or NotebookLM could not give their words this "
            "time. The next run tries them again.\n"
        )
        lines += [f"  - {videos[v]}" for v in missing]
    lines.append("")
    if library.removed_outdated:
        lines.append("## Left out as outdated\n")
        lines.append(
            "News older than a year, and rules or figures a newer video replaced, are left out of the "
            "books:\n"
        )
        for p in library.removed_outdated:
            when = date_label(p.published, p.video_id in exact)
            lines.append(
                f'- {p.headline}: "{titles.get(p.video_id, p.title)}"' + (f" ({when})" if when else "")
            )
        lines.append("")
    if any(t.name == MISC for t in library.topics):
        lines.append(f"Passages that fit no topic are in the {MISC} book.\n")
    return Book(OVERVIEW_TITLE, "\n".join(lines))


def render_library(
    library: Library,
    channel: str,
    videos: dict[str, str],
    read: set[str],
    exact: set[str],
    dates: dict[str, str],
    parts: dict[str, dict[str, int]] | None = None,
) -> list[Book]:
    """Every source for the notebook: the overview first, then each topic book (and its parts).

    `parts` (topic -> section -> part number) is updated in place; see `render_topic`.
    """
    parts = {} if parts is None else parts
    books = [render_overview(library, channel, videos, read, videos, exact, dates)]
    for topic in library.topics:
        books += render_topic(topic, channel, videos, exact, parts.setdefault(topic.name, {}))
    for gone in set(parts) - {t.name for t in library.topics}:
        del parts[gone]
    return books
