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
from .errors import AlchemyError, ClaudeError
from .knowledge import VideoLabels, apply_punctuation

logger = logging.getLogger(__name__)

# 2: answers are JSON matching a schema, and passage-number maps became lists of pairs.
LIBRARY_PROMPT_VERSION = "2"
MISC = "Miscellaneous"
OTHER_POINTS = "Other points from this video"
ASSIGN_BATCH = 300
HEADLINE_SAMPLE = 600
NEWS_DROP_MONTHS = 12  # news and events older than this are left out
LABEL_AFTER_MONTHS = 18  # rules and figures older than this are kept, marked "as of"
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
    sorted_by_ai: bool = True  # False on the no-key path: grouped by title words only


# --- passages -------------------------------------------------------------------------------------


def build_passages(
    video_id: str, title: str, published: str, chunks: list[Chunk], labels: VideoLabels | None
) -> list[Passage]:
    """The video's kept segments as passages; kept chunks no segment covers become one more."""
    by_n = {c.n: c for c in chunks if c.kept}
    out: list[Passage] = []
    covered: set[int] = set()
    for seg in labels.segments if labels else []:
        # Only chunks the model was shown are decided by its segment; one it never saw stays kept.
        span = [n for n in range(seg.start, seg.end + 1) if labels is not None and labels.covers(n)]
        covered.update(span)
        if not seg.keep:
            continue
        kept = [by_n[n] for n in span if n in by_n]
        if not kept:
            continue
        also = sorted({v for c in kept for v in c.also_in})
        raw = " ".join(c.text for c in kept)
        # The speaker's words, every one; punctuation and capitals come from the model's version
        # wherever its words line up with them.
        text = apply_punctuation(raw, seg.text) if seg.text else raw
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

    def drop_prefix(self, prefix: str) -> None:
        """Forget every answer whose key starts with `prefix`, and save."""
        for key in [k for k in self.data if k.startswith(prefix)]:
            del self.data[key]
        self.save()

    def put(self, key: str, value: Any) -> None:
        """Store an answer and save at once: a run stopped part-way (a limit) keeps what it paid for."""
        self.data[key] = value
        self.save()

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


def _obj(**properties: Any) -> dict[str, Any]:
    """A JSON schema object whose properties are all required (structured outputs need this)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _list(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_SECTION = _obj(name=_STR, about=_STR)
_TOPICS = _list(_obj(name=_STR, sections=_list(_SECTION)))
TAXONOMY_SCHEMA = _obj(notebook_title=_STR, topics=_TOPICS)
EXTENSION_SCHEMA = _obj(topics=_TOPICS)
ASSIGN_SCHEMA = _obj(assignments=_list(_obj(passage=_INT, section=_INT)))
SUBSECTION_SCHEMA = _obj(subsections=_list(_STR))
CONSOLIDATE_SCHEMA = _obj(
    intro=_STR,
    order=_list(_INT),
    duplicates=_list(_obj(passage=_INT, keep=_INT)),
    superseded=_list(_INT),
)


def taxonomy_prompt(channel: str, titles: list[str], headlines: list[str]) -> str:
    return (
        f"You are organising everything the YouTube channel '{channel}' teaches into a small study "
        "library for Google NotebookLM: a few topic books, each with sections.\n"
        "Name the notebook (a short, specific title), then 3 to 12 topics that together cover the "
        "channel, each with 3 to 15 sections. Names are specific to this channel's subject (not "
        "generic labels like 'Tips' or 'General'); each section has a one-line description.\n"
        'Return JSON: {"notebook_title": "...", "topics": [{"name": "...", "sections": '
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
        'Return JSON: {"assignments": [{"passage": <passage number>, "section": <section number>}, ...]} '
        "with one entry for every passage number."
    )


def parse_assignments(text: str, passage_numbers: set[int], section_count: int) -> dict[int, int]:
    data = json.loads(text)
    pairs = data.get("assignments") if isinstance(data, dict) else None
    if not isinstance(pairs, list):
        raise ValueError("no 'assignments' list")
    out: dict[int, int] = {}
    for pair in pairs:
        try:
            p, s = int(pair["passage"]), int(pair["section"])
        except (KeyError, TypeError, ValueError):
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
        "3. Mark duplicates: a passage that makes the same point as another adds nothing. Pair it with "
        "the one to keep, preferring the newer and more complete one. A passage that adds any fact the "
        "other lacks is not a duplicate.\n"
        "4. Mark superseded passages: an older rule, figure or news item that a newer passage on the "
        "same point replaces.\n"
        'Return JSON: {"intro": "...", "order": [numbers], "duplicates": [{"passage": <n>, "keep": '
        "<kept n>}], "
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
    for pair in data.get("duplicates") or []:
        try:
            a, b = int(pair["passage"]), int(pair["keep"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= a < count and 0 <= b < count and a != b:
            dupes[a] = b
    # A chain or loop of duplicates must leave one survivor: drop a mapping whose target is itself
    # dropped, unless that target maps back.
    for a in list(dupes):
        if dupes.get(dupes[a]) is not None:
            dupes.pop(a)
    superseded = {int(n) for n in data.get("superseded") or [] if str(n).lstrip("-").isdigit()}
    # Only a time-sensitive passage is replaced, and only when a newer passage in the section stays.
    superseded = {
        n
        for n in superseded
        if 0 <= n < count
        and n not in dupes.values()
        and passages[n].kind in TIME_SENSITIVE
        and passages[n].published
        and any(
            p.published > passages[n].published
            for i, p in enumerate(passages)
            if i != n and i not in dupes and i not in superseded
        )
    }
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


# --- growing the map --------------------------------------------------------------------------------

SEP = "\x1f"  # between a topic and a section name in a stored placement
EXTEND_AT_LEAST = 5  # unplaced passages that make the map grow...
EXTEND_SHARE = 0.05  # ...or this share of all passages, whichever is larger
# New sections one extension may add. Unbounded, a large channel's answer ran past the output limit
# (live, 2026-11-13: 3,183 passages waiting) and nothing was added. Changing the prompt changes
# EXTEND_VERSION, so an extension remembered for the old prompt is asked again.
EXTEND_MAX_SECTIONS = 30
EXTEND_VERSION = "2"
MAP_MAX_TOKENS = 16384  # the topic map and an extension: names and one-line descriptions only
SUBSECTION_AT = 200  # passages in one section before it is divided into subsections
OTHER_SUBSECTION = "Other points"


def _map_to_json(topics: list[Topic]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "sections": [{"name": s.name, "about": s.about} for s in t.sections]} for t in topics
    ]


def _map_from_json(data: list[dict[str, Any]]) -> list[Topic]:
    return [
        Topic(
            str(t["name"]),
            [Section(str(s["name"]), about=str(s.get("about", ""))) for s in t.get("sections", [])],
        )
        for t in data
    ]


def extend_prompt(channel: str, topics: list[Topic], headlines: list[str]) -> str:
    current = "\n".join(f"- {t.name}: " + "; ".join(s.name for s in t.sections) for t in topics)
    return (
        f"The study library for the YouTube channel '{channel}' has these topics and sections:\n{current}\n\n"
        "The points below fit none of them. Add only what they need: new sections inside an existing "
        "topic (use its exact name), or a new topic with its sections. Do not rename, remove or repeat "
        f"anything that exists. Add at most {EXTEND_MAX_SECTIONS} new sections in all, each for a group of "
        "related points, with a one-line description; list only the new sections.\n"
        'Return JSON: {"topics": [{"name": "...", "sections": [{"name": "...", "about": "..."}]}]}\n\n'
        "Points:\n" + "\n".join(f"- {h[:120]}" for h in _sample(headlines, HEADLINE_SAMPLE))
    )


def parse_extension(text: str) -> list[Topic]:
    data = json.loads(text)
    out: list[Topic] = []
    for raw in data.get("topics") or [] if isinstance(data, dict) else []:
        name = " ".join(str(raw.get("name", "")).split())[:80] if isinstance(raw, dict) else ""
        sections = [
            Section(
                " ".join(str(s.get("name", "")).split())[:80],
                about=" ".join(str(s.get("about", "")).split())[:200],
            )
            for s in raw.get("sections") or []
            if isinstance(s, dict) and str(s.get("name", "")).strip()
        ]
        if name and name.lower() != MISC.lower() and sections:
            out.append(Topic(name, sections))
    if not out:
        raise ValueError("no new sections")
    return out


def merge_map(topics: list[Topic], additions: list[Topic]) -> list[Topic]:
    """The existing map with new sections and topics added; nothing existing is renamed or lost."""
    merged = [Topic(t.name, [Section(s.name, about=s.about) for s in t.sections]) for t in topics]
    by_topic = {t.name.lower(): t for t in merged}
    for add in additions:
        target = by_topic.get(add.name.lower())
        if target is None:
            target = Topic(add.name, [])
            merged.append(target)
            by_topic[add.name.lower()] = target
        have = {s.name.lower() for s in target.sections}
        for s in add.sections:
            if s.name.lower() not in have:
                target.sections.append(s)
                have.add(s.name.lower())
    return [t for t in merged if t.sections]


def subsection_prompt(topic: str, section: Section) -> str:
    sample = _sample([p.headline for p in section.passages], HEADLINE_SAMPLE)
    return (
        f"The section '{section.name}' of the topic '{topic}' ({section.about}) has grown to "
        f"{len(section.passages)} points. Divide it into 3 to 10 subsections that together cover them; "
        "names are 2 to 6 words, specific, and do not repeat the section's name.\n"
        'Return JSON: {"subsections": ["...", ...]}\n\n'
        "Points (a sample):\n" + "\n".join(f"- {h[:120]}" for h in sample)
    )


def parse_subsections(text: str) -> list[str]:
    data = json.loads(text)
    names: list[str] = []
    for item in data.get("subsections") or [] if isinstance(data, dict) else []:
        name = " ".join(str(item).split())[:80]
        if (
            name
            and name.lower() not in {n.lower() for n in names}
            and name.lower() != OTHER_SUBSECTION.lower()
        ):
            names.append(name)
    if len(names) < 2:
        raise ValueError(f"only {len(names)} usable subsections")
    return names


class LibraryBuilder:
    """Sorts passages into topics and sections and consolidates each section, with an AI answerer.

    The library grows without reshuffling. Every answer is cached in `cache`:
    - **The topic map** is made once. When new passages fit nowhere, it is *extended*: new sections
      or topics are added for those passages only, and every existing one stays. Only `resort`
      rebuilds it.
    - **Each passage's place** is remembered by section name, so it survives a change to the map;
      only new passages are placed.
    - **A section past SUBSECTION_AT passages** is divided once into named subsections, and new
      passages are placed into those.
    - **Only sections whose passages changed** are consolidated again.
    """

    def __init__(self, ai: Any, cache: RequestCache, today: str, channel: str) -> None:
        self.ai, self.cache, self.today, self.channel = ai, cache, today, channel
        self.notices: list[str] = []
        self.requests = 0

    def _ask(self, prompt: str, schema: dict[str, Any], max_tokens: int = 16384) -> str:
        """The answer's JSON text. An answer that arrived but cannot be used (refused or cut off)
        raises ValueError, like one that does not parse, so both are handled the same way."""
        self.requests += 1
        try:
            return str(self.ai.generate(prompt, schema=schema, max_tokens=max_tokens).text)
        except ClaudeError as exc:
            if exc.unusable:
                raise ValueError(exc.detail) from None
            raise

    def build(
        self,
        passages: list[Passage],
        titles: list[str],
        *,
        resort: bool = False,
        progress: Any = None,
    ) -> Library:
        kept, removed_old = apply_date_rules(passages, self.today)
        if not kept:
            # Nothing to sort: a map made from titles alone would be kept for good, so none is asked for.
            return Library(self.channel, [], removed_outdated=removed_old)
        if resort:
            self.cache.put(self._map_key, None)
            self.cache.put(self._place_key, {})
            self.cache.drop_prefix(f"sub:{self.channel}{SEP}")
        title, topics = self._topic_map(kept, titles)
        placement = self._place(kept, topics, progress)
        unplaced = [p for p in kept if not placement.get(p.id)]
        if len(unplaced) > max(EXTEND_AT_LEAST, len(kept) * EXTEND_SHARE) and self._new_unplaced(unplaced):
            grown = self._extend(topics, unplaced)
            if grown is None:
                pass  # no answer came; the next run asks again
            elif grown is topics:
                self._mark_extended(unplaced)  # the answer came and added nothing: not asked again
            else:
                topics = grown
                waiting = {p.id for p in unplaced}
                self.cache.put(self._place_key, {k: v for k, v in placement.items() if k not in waiting})
                placement = self._place(kept, topics, progress)
                self._mark_extended([p for p in kept if not placement.get(p.id)])
        by_name = {f"{t.name}{SEP}{s.name}": s for t in topics for s in t.sections}
        misc = Section(MISC, about="Points that fit no other section")
        for p in kept:
            section = by_name.get(placement.get(p.id, ""))
            (section.passages if section is not None else misc.passages).append(p)
        if misc.passages:
            topics.append(Topic(MISC, [misc]))
        for topic in topics:
            topic.sections = [part for s in topic.sections for part in self._divide(topic, s)]
        library = Library(title, topics, removed_outdated=removed_old, unplaced=len(misc.passages))
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

    # -- the topic map --

    @property
    def _map_key(self) -> str:
        return f"map:{self.channel}"

    @property
    def _place_key(self) -> str:
        return f"placed:{self.channel}"

    def _topic_map(self, passages: list[Passage], titles: list[str]) -> tuple[str, list[Topic]]:
        """The notebook's title and topics, asked for once. An answer that cannot be used is remembered
        for the passages it saw, so it is not paid for again until they change or `--resort`."""
        stored = self.cache.get(self._map_key)
        digest = self._digest(passages)
        if stored is None or (stored.get("unusable_for") and stored["unusable_for"] != digest):
            try:
                text = self._ask(
                    taxonomy_prompt(self.channel, titles, [p.headline for p in passages]),
                    TAXONOMY_SCHEMA,
                    MAP_MAX_TOKENS,
                )
                name, topics = parse_taxonomy(text)
            except (ValueError, TypeError) as exc:
                stored = {"unusable_for": digest, "reason": str(exc)[:200]}
                self.cache.put(self._map_key, stored)
            else:
                stored = {"title": name or self.channel, "topics": _map_to_json(topics)}
                self.cache.put(self._map_key, stored)
        if "topics" not in stored:
            raise AlchemyError(
                f"The AI's topic map for {self.channel} could not be used ({stored.get('reason', '')}). "
                "The labels are saved; nothing was uploaded.",
                hint="Run the same command with --resort to ask again, or with --offline to build the books "
                "without AI.",
            )
        return str(stored.get("title") or self.channel), _map_from_json(stored["topics"])

    @staticmethod
    def _digest(unplaced: list[Passage]) -> str:
        return hashlib.sha256("\n".join(sorted(p.id for p in unplaced)).encode()).hexdigest()[:16]

    @staticmethod
    def _extend_digest(unplaced: list[Passage]) -> str:
        """The passages an extension was asked about, and the prompt version it was asked with."""
        return f"{EXTEND_VERSION}:{LibraryBuilder._digest(unplaced)}"

    def _new_unplaced(self, unplaced: list[Passage]) -> bool:
        """Whether these unplaced passages differ from the set the map was last extended for.

        The map is extended once per set, so passages that still fit nowhere afterwards do not
        cost another request on every run.
        """
        return (self.cache.get(self._map_key) or {}).get("extended_for") != self._extend_digest(unplaced)

    def _mark_extended(self, unplaced: list[Passage]) -> None:
        stored = self.cache.get(self._map_key) or {"title": self.channel}
        self.cache.put(self._map_key, {**stored, "extended_for": self._extend_digest(unplaced)})

    def _extend(self, topics: list[Topic], unplaced: list[Passage]) -> list[Topic] | None:
        """Add sections (or topics) for passages that fit nowhere; every existing one stays.

        Returns the grown map, `topics` itself when an answer came but added nothing usable, or
        None when no answer came (a network or server error), so the next run asks again.
        """
        try:
            text = self._ask(
                extend_prompt(self.channel, topics, [p.headline for p in unplaced]),
                EXTENSION_SCHEMA,
                MAP_MAX_TOKENS,
            )
            additions = parse_extension(text)
        except ClaudeError as exc:
            if exc.stop_run:
                raise
            self.notices.append(
                f"New topics could not be added ({exc.detail}); those passages wait in {MISC}."
            )
            return None
        except (ValueError, TypeError) as exc:
            self.notices.append(f"New topics could not be added ({exc}); those passages wait in {MISC}.")
            return topics
        merged = merge_map(topics, additions)
        if sum(len(t.sections) for t in merged) == sum(len(t.sections) for t in topics):
            self.notices.append(f"No new topics fit the unplaced passages; they wait in {MISC}.")
            return topics
        stored = self.cache.get(self._map_key) or {"title": self.channel}
        self.cache.put(
            self._map_key,
            {**stored, "topics": _map_to_json(merged)},
        )
        return merged

    def _place(self, passages: list[Passage], topics: list[Topic], progress: Any) -> dict[str, str]:
        """Each passage's section, as "topic<SEP>section", "" when the model placed it nowhere.

        Remembered by name, so a passage keeps its place when the map is extended.
        """
        sections = [(t, s) for t in topics for s in t.sections]
        names = [f"{t.name}{SEP}{s.name}" for t, s in sections]
        placed: dict[str, str] = {
            k: v for k, v in (self.cache.get(self._place_key) or {}).items() if v in names or v == ""
        }
        todo = [p for p in passages if p.id not in placed]
        if todo:
            # Passages placed nowhere earlier ride along with new ones: the map may fit them now, and
            # they are few (more than EXTEND_SHARE of them makes the map grow instead).
            todo += [p for p in passages if placed.get(p.id) == ""]
        listing = "\n".join(f"{i}: {t.name} / {s.name} — {s.about}" for i, (t, s) in enumerate(sections))
        batches = [todo[i : i + ASSIGN_BATCH] for i in range(0, len(todo), ASSIGN_BATCH)]
        for done, batch in enumerate(batches, 1):
            numbered = list(enumerate(batch))
            mapping = self._assign(listing, numbered, len(sections), batch)
            if mapping is None:
                continue
            for n, p in numbered:
                # A passage the model placed nowhere is remembered as "": it is asked about again only
                # with the next new passages, or when the map is extended.
                placed[p.id] = names[mapping[n]] if n in mapping else ""
            self.cache.put(self._place_key, placed)
            if progress:
                progress("Sorting passages into sections", done, len(batches))
        return placed

    def _assign(
        self, listing: str, numbered: list[tuple[int, Passage]], count: int, batch: list[Passage]
    ) -> dict[int, int] | None:
        try:
            return parse_assignments(
                self._ask(assign_prompt(listing, numbered), ASSIGN_SCHEMA), {n for n, _ in numbered}, count
            )
        except ClaudeError as exc:
            if exc.stop_run:
                raise
            self.notices.append(
                f"{len(batch)} passages could not be placed ({exc.detail}); they are retried next run."
            )
            return None
        except (ValueError, TypeError) as exc:
            # The answer arrived and was paid for: its passages are remembered as placed nowhere and
            # asked about again only with the next new passages.
            self.notices.append(f"{len(batch)} passages could not be placed ({exc}); they wait in {MISC}.")
            return {}

    # -- subsections --

    def _divide(self, topic: Topic, section: Section) -> list[Section]:
        """A section past SUBSECTION_AT passages, as named subsections ("Section: Subsection").

        The subsection names are asked for once and kept; later passages are placed into them.
        """
        if len(section.passages) <= SUBSECTION_AT:
            return [section]
        key = f"sub:{self.channel}{SEP}{topic.name}{SEP}{section.name}"
        stored = self.cache.get(key) or {}
        names: list[str] = list(stored.get("names") or [])
        if not names:
            digest = self._digest(section.passages)
            if stored.get("unusable_for") == digest:
                return [section]  # asked already for these passages; the answer was unusable
            try:
                names = parse_subsections(
                    self._ask(subsection_prompt(topic.name, section), SUBSECTION_SCHEMA)
                )
            except ClaudeError as exc:
                if exc.stop_run:
                    raise
                self.notices.append(f"'{section.name}' was not divided ({exc.detail}).")
                return [section]
            except (ValueError, TypeError) as exc:
                self.notices.append(f"'{section.name}' was not divided ({exc}).")
                self.cache.put(key, {"unusable_for": digest})
                return [section]
            self.cache.put(key, {"names": names, "placed": {}})  # paid for: kept even if a limit stops
        placed: dict[str, int] = dict(stored.get("placed") or {})
        todo = [p for p in section.passages if p.id not in placed]
        listing = "\n".join(f"{i}: {name}" for i, name in enumerate(names))
        for start in range(0, len(todo), ASSIGN_BATCH):
            batch = todo[start : start + ASSIGN_BATCH]
            numbered = list(enumerate(batch))
            mapping = self._assign(listing, numbered, len(names), batch)
            if mapping is None:
                continue
            for n, p in numbered:
                placed[p.id] = mapping.get(n, -1)
            self.cache.put(key, {"names": names, "placed": placed})
        parts = [Section(f"{section.name}: {name}", about=section.about) for name in names]
        other = Section(f"{section.name}: {OTHER_SUBSECTION}", about=section.about)
        for p in section.passages:
            i = placed.get(p.id, -1)
            (parts[i] if 0 <= i < len(parts) else other).passages.append(p)
        return [s for s in [*parts, other] if s.passages]

    # -- sections --

    def _consolidate(self, topic: Topic, section: Section, library: Library) -> None:
        passages = section.passages
        if len(passages) == 1:
            return
        key = RequestCache.key(
            "consolidate",
            topic.name,
            section.name,
            self.today[:4],
            # Years, not days: approximate dates move a little every day, and the order does not.
            [(p.id, p.headline, p.kind, p.published[:4]) for p in passages],
        )
        text = self.cache.get(key)
        if isinstance(text, dict):  # an answer paid for earlier that could not be used
            self.notices.append(
                f"'{section.name}' kept its passages in their found order ({text.get('unusable')})."
            )
            return
        if text is None:
            try:
                text = self._ask(
                    consolidate_prompt(topic.name, section, self.today, passages), CONSOLIDATE_SCHEMA
                )
                apply_consolidation(text, list(passages))  # check it parses before caching
            except ClaudeError as exc:
                if exc.stop_run:
                    raise
                self.notices.append(
                    f"'{section.name}' kept its passages in their found order ({exc.detail})."
                )
                return
            except (ValueError, TypeError) as exc:
                self.notices.append(f"'{section.name}' kept its passages in their found order ({exc}).")
                self.cache.put(key, {"unusable": str(exc)[:200]})  # paid for: not asked again
                return
            self.cache.put(key, text)
        intro, ordered, duplicates, superseded = apply_consolidation(text, passages)
        section.intro, section.passages = intro, ordered
        library.removed_duplicates += duplicates
        library.removed_outdated += superseded
