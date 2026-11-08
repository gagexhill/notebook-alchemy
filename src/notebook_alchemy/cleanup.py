"""Local, free cleanup of transcripts before any AI sees them.

Captions are cleaned of caption noise and filler, split into short numbered chunks, and checked for
passages that repeat across videos (stock intros, pitches, reused segments) or within one. Only what
survives is sent to Gemini, so the paid step reads less; nothing here rewrites what was said.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

# Caption noise, not speech.
_NOISE_RE = re.compile(r"\[(?:music|applause|laughter|laughs|inaudible|silence|__)\]|>>|♪+", re.IGNORECASE)
# Hesitations that carry no meaning. Words such as "like" or "you know" are left alone: they can.
_FILLER_RE = re.compile(r"\b(?:um+|uh+|uhm+|erm+|hmm+)\b[,.]?\s*", re.IGNORECASE)
_STUTTER_RE = re.compile(r"\b(\w+)(?:\s+\1\b)+", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.?!])\s+(?=[\"'A-Z0-9])")
_WORD_RE = re.compile(r"[a-z0-9$%']+")

# Calls to action, sponsor reads and greetings: a short chunk built around one of these is dropped.
_BOILERPLATE_RE = re.compile(
    r"subscribe|notification bell|hit the bell|smash (?:that|the) like|like (?:this|the) video|"
    r"leave (?:a|your) comment|comment (?:below|down below)|link (?:is )?in the description|"
    r"sponsored by|this video is brought to you|use (?:my )?code|welcome back to (?:my|the) channel|"
    r"thanks for watching|thank you for watching|see you in the next (?:one|video)",
    re.IGNORECASE,
)
BOILERPLATE_MAX_WORDS = 30

CHUNK_WORDS = 45  # target words per chunk: one to three sentences
CHUNK_MAX_WORDS = 90
UNPUNCTUATED_BELOW = 1.0  # sentence marks per 100 words below which text is treated as unpunctuated
SHINGLE = 8  # words per shingle for duplicate detection
DUPLICATE_SHARE = 0.8  # share of a chunk's shingles already seen that makes it a duplicate
REPEAT_RUN_WORDS = 12  # a run this long, already said in another kept chunk, is taken out
_NORM_RE = re.compile(r"[^a-z0-9$%']+")


@dataclass
class Chunk:
    """A short numbered passage of one video, in the speaker's words."""

    video_id: str
    n: int
    text: str
    boilerplate: bool = False
    duplicate_of: str | None = None  # id of the kept chunk that says the same thing
    also_in: list[str] = field(default_factory=list)  # other videos that repeat this chunk

    @property
    def id(self) -> str:
        return f"{self.video_id}:{self.n}"

    @property
    def kept(self) -> bool:
        return not self.boilerplate and self.duplicate_of is None


def clean_text(text: str) -> str:
    """Caption noise, hesitations and stutters removed; spacing normalised. Words otherwise kept."""
    text = _NOISE_RE.sub(" ", text)
    text = _FILLER_RE.sub("", text)
    text = _STUTTER_RE.sub(r"\1", text)
    return " ".join(text.split())


def is_punctuated(text: str) -> bool:
    words = len(text.split())
    marks = len(re.findall(r"[.?!]", text))
    return words == 0 or marks / words * 100 >= UNPUNCTUATED_BELOW


def _is_boilerplate(sentence: str) -> bool:
    return len(sentence.split()) <= BOILERPLATE_MAX_WORDS and bool(_BOILERPLATE_RE.search(sentence))


def _pieces(text: str) -> list[tuple[str, bool]]:
    """(text, boilerplate) passages: sentences grouped to about CHUNK_WORDS, a short call to action
    or sponsor sentence kept apart; unpunctuated text in fixed windows (left for the AI to judge)."""
    text = clean_text(text)
    if not text:
        return []
    if not is_punctuated(text):
        words = text.split()
        return [(" ".join(words[i : i + CHUNK_WORDS]), False) for i in range(0, len(words), CHUNK_WORDS)]
    out: list[tuple[str, bool]] = []
    current: list[str] = []
    count = 0

    def flush() -> None:
        nonlocal current, count
        if current:
            out.append((" ".join(current), False))
        current, count = [], 0

    for sentence in (s for s in _SENTENCE_RE.split(text) if s.strip()):
        if _is_boilerplate(sentence):
            flush()
            out.append((sentence, True))
            continue
        size = len(sentence.split())
        if current and (count + size > CHUNK_MAX_WORDS or count >= CHUNK_WORDS):
            flush()
        current.append(sentence)
        count += size
    flush()
    return out


def split_chunks(text: str) -> list[str]:
    """Short passages of a transcript, cleaned, in order."""
    return [piece for piece, _boilerplate in _pieces(text)]


def chunk_video(video_id: str, text: str) -> list[Chunk]:
    """Numbered chunks for one transcript; a short call to action or sponsor read is marked."""
    return [Chunk(video_id, n, piece, boilerplate=bp) for n, (piece, bp) in enumerate(_pieces(text), 1)]


def _norm(word: str) -> str:
    return _NORM_RE.sub("", word.lower())


def _hash(words: list[str]) -> str:
    return hashlib.blake2b(" ".join(words).encode(), digest_size=8).hexdigest()


def _shingles(text: str) -> set[str]:
    words = [w for w in (_norm(w) for w in text.split()) if w]
    if len(words) < SHINGLE:
        return {_hash(words)} if words else set()
    return {_hash(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def _repeated_runs(text: str, owners: dict[str, Chunk], self_chunk: Chunk) -> tuple[str, list[Chunk]]:
    """The text with runs of REPEAT_RUN_WORDS or more words already said elsewhere taken out, and the
    chunks those runs were said in. The speaker's other words stay as they are."""
    original = text.split()
    kept_index = [i for i, w in enumerate(original) if _norm(w)]
    norm = [_norm(original[i]) for i in kept_index]
    covered = [False] * len(original)
    sources: list[Chunk] = []
    for i in range(len(norm) - SHINGLE + 1):
        owner = owners.get(_hash(norm[i : i + SHINGLE]))
        if owner is not None and owner is not self_chunk:
            sources.append(owner)
            for j in range(i, i + SHINGLE):
                covered[kept_index[j]] = True
    out: list[str] = []
    run: list[str] = []
    removed: list[Chunk] = []
    for i, word in enumerate(original):
        if covered[i]:
            run.append(word)
            continue
        if run:
            if len(run) < REPEAT_RUN_WORDS:
                out += run  # a short shared phrase is not a repeated passage
            else:
                removed = sources
            run = []
        out.append(word)
    if run:
        if len(run) < REPEAT_RUN_WORDS:
            out += run
        else:
            removed = sources
    return " ".join(out), removed


def mark_duplicates(videos: Iterable[tuple[str, list[Chunk]]]) -> int:
    """Keep each repeated passage once, newest video first, and return how many chunks were dropped.

    `videos` is (published date, chunks), in any order. A chunk that mostly repeats an earlier-kept
    one is marked a duplicate; a run of REPEAT_RUN_WORDS or more repeated words inside a chunk is
    taken out of it, and the rest of the chunk stays. The newest copy is the one kept, so a dated fact
    keeps its latest date, and it lists the other videos that said it.
    """
    owners: dict[str, Chunk] = {}  # shingle -> the kept chunk that first had it
    duplicates = 0
    for _published, chunks in sorted(videos, key=lambda pair: pair[0] or "", reverse=True):
        for chunk in chunks:
            if chunk.boilerplate:
                continue
            shingles = _shingles(chunk.text)
            if not shingles:
                continue
            seen = [owners[s] for s in shingles if s in owners and owners[s] is not chunk]
            if len(seen) / len(shingles) >= DUPLICATE_SHARE:
                counts = Counter(c.id for c in seen)
                keeper = next(c for c in seen if c.id == counts.most_common(1)[0][0])
                chunk.duplicate_of = keeper.id
                _cite(keeper, chunk.video_id)
                duplicates += 1
                continue
            if seen:
                text, sources = _repeated_runs(chunk.text, owners, chunk)
                if sources:
                    chunk.text = text
                    for keeper in {c.id: c for c in sources}.values():
                        _cite(keeper, chunk.video_id)
            for s in _shingles(chunk.text):
                owners.setdefault(s, chunk)
    return duplicates


def _cite(keeper: Chunk, video_id: str) -> None:
    if video_id != keeper.video_id and video_id not in keeper.also_in:
        keeper.also_in.append(video_id)
