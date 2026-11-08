"""From labelled chunks to a library: passages, topics and sections, consolidation and dating.

- **Passages:** each kept segment of a video becomes a passage in the speaker's words, with its
  headline, kind and date. Kept chunks the model left unlabelled are never lost: they become an
  "Other points from this video" passage.
- **Sorting:** one request names the notebook and its topics and sections, from headlines and titles;
  every passage is then placed by number, in batches. Misses go to a visible Miscellaneous section.
- **Consolidation:** one request per section returns a short introduction, the teaching order,
  duplicates and passages superseded by newer ones. Date rules are applied here, locally.

Every request is cached by what it saw, so a rerun pays only for what changed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cleanup import Chunk
from .knowledge import VideoLabels, same_words

logger = logging.getLogger(__name__)

LIBRARY_PROMPT_VERSION = "1"
MISC = "Miscellaneous"
OTHER_POINTS = "Other points from this video"
ASSIGN_BATCH = 300
HEADLINE_SAMPLE = 600
NEWS_DROP_MONTHS = 12  # news and events older than this are left out
LABEL_AFTER_MONTHS = 18  # rules and figures older than this are kept, marked "as of"
RESORT_GROWTH = 0.2  # new passages, as a share of those already sorted, that rebuild the topics
TIME_SENSITIVE = ("rule", "figure", "news")


@dataclass
class Passage:
    """A run of a video's kept chunks: one point, in the speaker's words."""

    id: str
    video_id: str
    title: str  # the video's title
    published: str  # YYYY-MM-DD, or "" when unknown
    headline: str
    kind: str
    note: str
    text: str
    also_in: list[str] = field(default_factory=list)  # other videos that said the same thing
    as_of: str = ""  # set when a time-sensitive passage is old: "2023"

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass
class Section:
    name: str
    about: str = ""
    intro: str = ""
    passages: list[Passage] = field(default_factory=list)


@dataclass
class Topic:
    name: str
    sections: list[Section] = field(default_factory=list)

    @property
    def words(self) -> int:
        return sum(p.words for s in self.sections for p in s.passages)


@dataclass
class Library:
    notebook_title: str
    topics: list[Topic]
    removed_outdated: list[Passage] = field(default_factory=list)
    removed_duplicates: int = 0
    unplaced: int = 0  # passages the model did not place (kept under Miscellaneous)


# --- passages -------------------------------------------------------------------------------------


def build_passages(
    video_id: str, title: str, published: str, chunks: list[Chunk], labels: VideoLabels | None
) -> list[Passage]:
    """The video's kept segments as passages; kept chunks no segment covers become one more."""
    by_n = {c.n: c for c in chunks if c.kept}
    out: list[Passage] = []
    covered: set[int] = set()
    for seg in labels.segments if labels else []:
        span = range(seg.start, seg.end + 1)
        covered.update(span)
        if not seg.keep:
            continue
        kept = [by_n[n] for n in span if n in by_n]
        if not kept:
            continue
        also = sorted({v for c in kept for v in c.also_in})
        raw = " ".join(c.text for c in kept)
        # The model's punctuated version is used only when it kept the speaker's words.
        text = seg.text if seg.text and same_words(raw, seg.text) else raw
        out.append(
            Passage(
                id=f"{video_id}:{seg.start}",
                video_id=video_id,
                title=title,
                published=published,
                headline=seg.headline or "Point",
                kind=seg.kind,
                note=seg.note,
                text=text,
                also_in=also,
            )
        )
    leftover = [c for n, c in sorted(by_n.items()) if n not in covered]
    if leftover:
        out.append(
            Passage(
                id=f"{video_id}:other",
                video_id=video_id,
                title=title,
                published=published,
                headline=OTHER_POINTS,
                kind="lasting",
                note="",
                text=" ".join(c.text for c in leftover),
                also_in=sorted({v for c in leftover for v in c.also_in}),
            )
        )
    return out


# --- dates ----------------------------------------------------------------------------------------


def months_between(earlier: str, today: str) -> int | None:
    try:
        a, b = dt.date.fromisoformat(earlier), dt.date.fromisoformat(today)
    except ValueError:
        return None
    return (b.year - a.year) * 12 + (b.month - a.month)


def apply_date_rules(passages: list[Passage], today: str) -> tuple[list[Passage], list[Passage]]:
    """(kept, removed). Old news is removed; old rules and figures are kept, marked "as of".

    A passage with no date is never removed or marked: its age is unknown.
    """
    kept: list[Passage] = []
    removed: list[Passage] = []
    for p in passages:
        age = months_between(p.published, today) if p.published else None
        if age is None or p.kind not in TIME_SENSITIVE:
            kept.append(p)
        elif p.kind == "news" and age > NEWS_DROP_MONTHS:
            removed.append(p)
        else:
            if age > LABEL_AFTER_MONTHS:
                p.as_of = p.published[:4]
            kept.append(p)
    return kept, removed


# --- the cache of requests ------------------------------------------------------------------------


class RequestCache:
    """Answers to sorting and consolidation requests, keyed by everything the request saw."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable library cache %s: %s", path, exc)
            self.data = {}

    @staticmethod
    def key(*parts: Any) -> str:
        blob = json.dumps([LIBRARY_PROMPT_VERSION, *parts], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def put(self, key: str, value: Any) -> None:
        self.data[key] = value

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save the library cache %s: %s", self.path, exc)


# --- sorting --------------------------------------------------------------------------------------


def _sample(items: list[str], size: int) -> list[str]:
    if len(items) <= size:
        return items
    step = len(items) / size
    return [items[int(i * step)] for i in range(size)]


def taxonomy_prompt(channel: str, titles: list[str], headlines: list[str]) -> str:
    return (
        f"You are organising everything the YouTube channel '{channel}' teaches into a small study "
        "library for Google NotebookLM: a few topic books, each with sections.\n"
        "Name the notebook (a short, specific title), then 3 to 12 topics that together cover the "
        "channel, each with 3 to 15 sections. Names are specific to this channel's subject (not "
        "generic labels like 'Tips' or 'General'); each section has a one-line description.\n"
        'Return only JSON: {"notebook_title": "...", "topics": [{"name": "...", "sections": '
        '[{"name": "...", "about": "..."}]}]}\n\n'
        "Video titles (a sample):\n"
        + "\n".join(f"- {t[:120]}" for t in _sample(titles, 200))
        + "\n\nPoints the videos make (headlines, a sample):\n"
        + "\n".join(f"- {h[:120]}" for h in _sample(headlines, HEADLINE_SAMPLE))
    )


def parse_taxonomy(text: str) -> tuple[str, list[Topic]]:
    data = json.loads(text)
    title = " ".join(str(data.get("notebook_title", "")).split())[:100]
    topics: list[Topic] = []
    seen_topics: set[str] = set()
    for raw in data.get("topics") or []:
        name = " ".join(str(raw.get("name", "")).split())[:80] if isinstance(raw, dict) else ""
        if not name or name.lower() in seen_topics or name.lower() == MISC.lower():
            continue
        seen_topics.add(name.lower())
        sections: list[Section] = []
        seen: set[str] = set()
        for s in raw.get("sections") or []:
            sname = " ".join(str(s.get("name", "")).split())[:80] if isinstance(s, dict) else ""
            if sname and sname.lower() not in seen:
                seen.add(sname.lower())
                sections.append(Section(sname, about=" ".join(str(s.get("about", "")).split())[:200]))
        if sections:
            topics.append(Topic(name, sections))
    if len(topics) < 1:
        raise ValueError("no usable topics")
    return title, topics


def assign_prompt(listing: str, passages: list[tuple[int, Passage]]) -> str:
    lines = "\n".join(f"{n}: {p.headline} — {p.text[:160]}" for n, p in passages)
    return (
        "Place each numbered passage in exactly one section of the library.\n"
        f"Sections:\n{listing}\n\nPassages:\n{lines}\n\n"
        'Return only JSON: {"assignments": {"<passage number>": <section number>, ...}} covering '
        "every passage number."
    )


def parse_assignments(text: str, passage_numbers: set[int], section_count: int) -> dict[int, int]:
    data = json.loads(text)
    mapping = data.get("assignments") if isinstance(data, dict) else None
    if not isinstance(mapping, dict):
        raise ValueError("no 'assignments' object")
    out: dict[int, int] = {}
    for key, value in mapping.items():
        try:
            p, s = int(key), int(value)
        except (TypeError, ValueError):
            continue
        if p in passage_numbers and 0 <= s < section_count:
            out[p] = s
    return out


# --- consolidation --------------------------------------------------------------------------------


def _first_sentence(text: str, words: int = 30) -> str:
    return " ".join(text.split()[:words])


def consolidate_prompt(topic: str, section: Section, today: str, passages: list[Passage]) -> str:
    lines = "\n".join(
        f"{i}: [{p.kind}, {p.published or 'date unknown'}] {p.headline} — {_first_sentence(p.text)}"
        for i, p in enumerate(passages)
    )
    return (
        f"Section '{section.name}' of the topic '{topic}' ({section.about}). Today is {today}.\n"
        "The numbered passages below are in the speakers' own words; you will not rewrite them.\n"
        "1. Write a 2 to 3 sentence introduction to the section, in plain words, for a reader new to it.\n"
        "2. Give the passage numbers in teaching order: basics before details, steps in sequence.\n"
        "3. Mark duplicates: a passage that makes the same point as another adds nothing. Map it to "
        "the one to keep, preferring the newer and more complete one. A passage that adds any fact the "
        "other lacks is not a duplicate.\n"
        "4. Mark superseded passages: an older rule, figure or news item that a newer passage on the "
        "same point replaces.\n"
        'Return only JSON: {"intro": "...", "order": [numbers], "duplicates": {"<n>": <kept n>}, '
        '"superseded": [numbers]}\n\n'
        f"Passages:\n{lines}"
    )


def apply_consolidation(text: str, passages: list[Passage]) -> tuple[str, list[Passage], int, list[Passage]]:
    """(intro, ordered kept passages, duplicates removed, superseded removed). Nothing is lost by
    accident: a passage the answer does not mention keeps its place at the end."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    count = len(passages)
    dupes: dict[int, int] = {}
    for k, v in (data.get("duplicates") or {}).items():
        try:
            a, b = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= a < count and 0 <= b < count and a != b:
            dupes[a] = b
    # A chain or loop of duplicates must leave one survivor: drop a mapping whose target is itself
    # dropped, unless that target maps back.
    for a in list(dupes):
        if dupes.get(dupes[a]) is not None:
            dupes.pop(a)
    superseded = {int(n) for n in data.get("superseded") or [] if str(n).lstrip("-").isdigit()}
    superseded = {n for n in superseded if 0 <= n < count and n not in dupes.values()}
    for a, b in dupes.items():
        keeper = passages[b]
        cited = [passages[a].video_id, *passages[a].also_in]
        for vid in cited:
            if vid != keeper.video_id and vid not in keeper.also_in:
                keeper.also_in.append(vid)
    removed = set(dupes) | superseded
    order: list[int] = []
    for n in data.get("order") or []:
        try:
            i = int(n)
        except (TypeError, ValueError):
            continue
        if 0 <= i < count and i not in removed and i not in order:
            order.append(i)
    order += [i for i in range(count) if i not in removed and i not in order]
    intro = " ".join(str(data.get("intro", "")).split())
    return intro, [passages[i] for i in order], len(dupes), [passages[i] for i in sorted(superseded)]


# --- building the library -------------------------------------------------------------------------


class LibraryBuilder:
    """Sorts passages into topics and sections and consolidates each section, with Gemini.

    Every answer is cached in `cache` by what the request saw: the topics are rebuilt only when
    none exist, `resort` is asked for, or new passages grow the library by more than RESORT_GROWTH;
    only unplaced passages are placed; only sections whose passages changed are consolidated again.
    """

    def __init__(self, gemini: Any, cache: RequestCache, today: str, channel: str) -> None:
        self.gemini, self.cache, self.today, self.channel = gemini, cache, today, channel
        self.notices: list[str] = []
        self.requests = 0

    def _ask(self, prompt: str, max_tokens: int = 16384) -> str:
        self.requests += 1
        result = self.gemini.generate(
            prompt, json_mode=True, max_output_tokens=max_tokens, allow_fallback=False
        )
        return result.text

    def build(
        self,
        passages: list[Passage],
        titles: list[str],
        *,
        resort: bool = False,
        progress: Any = None,
    ) -> Library:
        kept, removed_old = apply_date_rules(passages, self.today)
        title, topics, version = self._taxonomy(kept, titles, resort)
        sections = [(t, s) for t in topics for s in t.sections]
        placement = self._place(kept, sections, version, progress)
        misc = Section(MISC, about="Points that fit no other section")
        unplaced = 0
        for p in kept:
            index = placement.get(p.id)
            if index is None or index < 0:
                misc.passages.append(p)
                unplaced += 1
            else:
                sections[index][1].passages.append(p)
        if misc.passages:
            topics.append(Topic(MISC, [misc]))
        library = Library(title, topics, removed_outdated=removed_old, unplaced=unplaced)
        work = [(t, s) for t in topics for s in t.sections if s.passages]
        for done, (topic, section) in enumerate(work, 1):
            self._consolidate(topic, section, library)
            if progress:
                progress("Consolidating sections", done, len(work))
        for topic in topics:
            topic.sections = [s for s in topic.sections if s.passages]
        library.topics = [t for t in topics if t.sections]
        self.cache.save()
        return library

    # -- topics --

    def _taxonomy(
        self, passages: list[Passage], titles: list[str], resort: bool
    ) -> tuple[str, list[Topic], str]:
        stored = self.cache.get(f"taxonomy:{self.channel}")
        grown = stored and len(passages) > stored.get("sorted", 0) * (1 + RESORT_GROWTH)
        if stored and not resort and not grown:
            text = stored["answer"]
        else:
            text = self._ask(taxonomy_prompt(self.channel, titles, [p.headline for p in passages]), 8192)
            parse_taxonomy(text)  # raises on an unusable answer before it is cached
            self.cache.put(f"taxonomy:{self.channel}", {"answer": text, "sorted": len(passages)})
        name, topics = parse_taxonomy(text)
        version = RequestCache.key("taxonomy", text)
        return name or self.channel, topics, version

    def _place(
        self, passages: list[Passage], sections: list[tuple[Topic, Section]], version: str, progress: Any
    ) -> dict[str, int]:
        cache_key = f"placement:{version}"
        placed: dict[str, int] = dict(self.cache.get(cache_key) or {})
        todo = [p for p in passages if p.id not in placed]
        listing = "\n".join(f"{i}: {t.name} / {s.name} — {s.about}" for i, (t, s) in enumerate(sections))
        batches = [todo[i : i + ASSIGN_BATCH] for i in range(0, len(todo), ASSIGN_BATCH)]
        for done, batch in enumerate(batches, 1):
            numbered = list(enumerate(batch))
            try:
                mapping = parse_assignments(
                    self._ask(assign_prompt(listing, numbered)), {n for n, _ in numbered}, len(sections)
                )
            except (ValueError, TypeError) as exc:
                self.notices.append(
                    f"{len(batch)} passages could not be placed ({exc}); they are retried next run."
                )
                continue
            for n, p in numbered:
                # Asked once: a passage the model did not place stays in Miscellaneous (-1) and is
                # not asked about again, so a rerun costs nothing for it.
                placed[p.id] = mapping.get(n, -1)
            self.cache.put(cache_key, placed)
            if progress:
                progress("Sorting passages into sections", done, len(batches))
        return placed

    # -- sections --

    def _consolidate(self, topic: Topic, section: Section, library: Library) -> None:
        passages = section.passages
        if len(passages) == 1:
            return
        key = RequestCache.key(
            "consolidate",
            topic.name,
            section.name,
            self.today[:7],
            [(p.id, p.headline, p.kind, p.published) for p in passages],
        )
        text = self.cache.get(key)
        if text is None:
            try:
                text = self._ask(consolidate_prompt(topic.name, section, self.today, passages))
                apply_consolidation(text, list(passages))  # check it parses before caching
            except (ValueError, TypeError) as exc:
                self.notices.append(f"'{section.name}' kept its passages in their found order ({exc}).")
                return
            self.cache.put(key, text)
        intro, ordered, duplicates, superseded = apply_consolidation(text, passages)
        section.intro, section.passages = intro, ordered
        library.removed_duplicates += duplicates
        library.removed_outdated += superseded
