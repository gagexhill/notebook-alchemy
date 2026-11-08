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


def render_topic(topic: Topic, channel: str, titles: dict[str, str], exact: set[str]) -> list[Book]:
    """The topic as one book, or as parts when it is longer than SOURCE_WORD_LIMIT words."""
    blocks: list[str] = []
    for s in topic.sections:
        text = _section(s, titles, exact)
        if len(text.split()) <= SOURCE_WORD_LIMIT:
            blocks.append(text)
            continue
        # A single section too large for one source is cut between passages.
        current = f"## {s.name}\n\n" + (f"{s.intro}\n\n" if s.intro else "")
        for p in s.passages:
            piece = _passage(p, titles, exact) + "\n"
            if len((current + piece).split()) > SOURCE_WORD_LIMIT:
                blocks.append(current)
                current = f"## {s.name} (continued)\n\n"
            current += piece
        blocks.append(current)
    books: list[Book] = []
    body: list[str] = []
    for block in blocks:
        if body and len(" ".join([*body, block]).split()) > SOURCE_WORD_LIMIT:
            books.append(Book("", "\n".join(body)))
            body = []
        body.append(block)
    if body:
        books.append(Book("", "\n".join(body)))
    for i, book in enumerate(books, 1):
        title = topic.name if i == 1 else f"{topic.name} (Part {i})"
        book.title = title
        book.content = f"# {title}\n\n{_intro(channel)}\n{book.content}"
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
) -> list[Book]:
    """Every source for the notebook: the overview first, then each topic book (and its parts)."""
    books = [render_overview(library, channel, videos, read, videos, exact, dates)]
    for topic in library.topics:
        books += render_topic(topic, channel, videos, exact)
    return books
