"""Labelling: Gemini reads each video's numbered chunks and returns labels, never rewritten text.

For every run of chunks the model says keep or drop; for kept runs it adds a short headline, the
kind of information (how long it stays true) and, only when a passage depends on context, a short
editor's note. The output is a few percent of the transcript, which is what keeps the paid step
cheap, and the words in the textbook stay the speaker's own. Labels are cached per video, keyed by
the transcript, the prompt version and the model, so a video is labelled once.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .cleanup import Chunk, is_punctuated
from .errors import GeminiError
from .gemini import GeminiResult

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1"
KINDS = ("lasting", "rule", "news", "figure")
LOW_KEEP_SHARE = 0.2  # below this share of kept chunks a video is labelled once more, keeping when unsure
WINDOW_WORDS = 70_000  # a very long video is labelled in windows of about this many words
# When the model also returns punctuated text, its answer is about as long as the window.
PUNCTUATE_WINDOW_WORDS = 5_000
PUNCTUATE_ALIGN_MIN = 0.8  # below this the model's text is not the same passage, and is not used
NOTE_MAX_WORDS = 25
HEADLINE_MAX_WORDS = 14
LABEL_MAX_OUTPUT_TOKENS = 16384
LABEL_TIMEOUT = 300.0  # seconds; an answer that carries punctuated words is long

SYSTEM = (
    "You label transcripts for a study library. You never rewrite or summarise the speaker's words; "
    "you only decide which numbered chunks are worth keeping, group them into points, and describe each "
    "point. Answer with JSON only."
)

RULES = """Keep (in the speaker's words):
- step-by-step procedures and how-tos;
- specific facts: names of programs, agencies, systems, forms and codes; requirements, eligibility and
  thresholds; dollar amounts, deadlines and dates; websites and tools named;
- definitions and explanations of concepts, and the reasoning behind advice;
- frameworks, checklists and rules of thumb;
- examples, case stories and their outcomes, with numbers;
- mistakes, warnings and how to fix them;
- answers to viewer questions.
Drop:
- greetings, intros, outros and "in this video I'll..." previews;
- like, subscribe and bell requests; sponsor reads; promotion of the creator's own courses or coaching
  (unless the offer itself is the subject);
- filler, repetition of a point already made, small talk, jokes and off-topic tangents;
- pointers to links that carry no content.
When unsure whether something is useful, KEEP it."""

KIND_HELP = """kind, for each kept point:
- "lasting": a principle or explanation that stays true;
- "rule": a procedure or requirement tied to a website, regulation, program or system as it was then;
- "news": an event or announcement (a shutdown, an executive order, a policy just changed);
- "figure": prices, grant amounts, deadlines, rates or other numbers that change."""


@dataclass
class Segment:
    """A run of chunks (inclusive) with one decision."""

    start: int
    end: int
    keep: bool
    headline: str = ""
    kind: str = "lasting"
    note: str = ""
    text: str = ""  # the kept chunks' own words with punctuation added (unpunctuated captions only)


@dataclass
class VideoLabels:
    video_id: str
    key: str
    model: str
    segments: list[Segment] = field(default_factory=list)
    invalid: int = 0  # segments the model returned that pointed outside the chunks
    relabelled: bool = False
    # The chunk numbers the model was shown. A chunk it never saw (hidden as a repeat at the time) is
    # not covered by a segment, even when a segment's range spans it.
    seen: list[int] = field(default_factory=list)

    def covers(self, n: int) -> bool:
        return not self.seen or n in self.seen

    def segment_for(self, n: int) -> Segment | None:
        for seg in self.segments:
            if seg.start <= n <= seg.end:
                return seg
        return None

    def kept_share(self, chunk_numbers: list[int]) -> float:
        if not chunk_numbers:
            return 1.0
        kept = sum(1 for n in chunk_numbers if (s := self.segment_for(n)) is None or s.keep)
        return kept / len(chunk_numbers)


def label_key(video_id: str, transcript: str, model: str) -> str:
    """Same transcript, prompt and model: same labels. Anything else labels again."""
    digest = hashlib.sha256(f"{PROMPT_VERSION}\x00{model}\x00{video_id}\x00{transcript}".encode())
    return digest.hexdigest()[:32]


class KnowledgeStore:
    """Labels per video as JSON under `root` (by default ~/.notebook-alchemy/knowledge)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, video_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in video_id)
        return self.root / f"{safe}.json"

    def get(self, video_id: str, key: str) -> VideoLabels | None:
        path = self._path(video_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable labels %s: %s", path, exc)
            return None
        if data.get("key") != key:
            return None
        return VideoLabels(
            video_id=video_id,
            key=key,
            model=str(data.get("model", "")),
            segments=[Segment(**s) for s in data.get("segments", [])],
            invalid=int(data.get("invalid", 0)),
            relabelled=bool(data.get("relabelled", False)),
            seen=[int(n) for n in data.get("seen", [])],
        )

    def put(self, labels: VideoLabels) -> None:
        path = self._path(labels.video_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(labels)), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save labels %s: %s", path, exc)


PUNCTUATE = (
    'These captions have no punctuation. For each kept segment also return "text": the exact words '
    "of its chunks, in order, with punctuation, capital letters and paragraph breaks added. Do not add, "
    "remove, reorder or change any word.\n"
)


def label_prompt(
    title: str,
    published: str,
    today: str,
    chunks: list[Chunk],
    *,
    keep_when_unsure: bool = False,
    punctuate: bool = False,
) -> str:
    """The request for one video (or one window of a long one)."""
    numbered = "\n".join(f"[{c.n}] {c.text}" for c in chunks)
    first, last = chunks[0].n, chunks[-1].n
    stricter = (
        "\nThis video was labelled before and almost everything was dropped. Keep every chunk that holds "
        "any fact, step, number, name, example or explanation; drop only greetings, calls to action and "
        "sponsor reads.\n"
        if keep_when_unsure
        else ""
    )
    return (
        f"Video: {title}\nPublished: {published or 'unknown'}\nToday: {today}\n\n"
        f"{RULES}\n\n{KIND_HELP}\n{stricter}{PUNCTUATE if punctuate else ''}\n"
        f"Group chunks {first} to {last} into consecutive segments that cover every chunk exactly once, "
        "in order. A kept segment is one coherent point (usually 1 to 8 chunks). Use the speaker's "
        "language for headlines and notes.\n"
        "Return only JSON:\n"
        '{"segments": [{"from": <first chunk>, "to": <last chunk>, "keep": true|false, '
        f'"headline": "<at most {HEADLINE_MAX_WORDS} words, kept segments only>", '
        '"kind": "lasting|rule|news|figure", '
        f'"note": "<optional, at most {NOTE_MAX_WORDS} words: context the passage needs to '
        'stand alone>"' + (', "text": "<the punctuated words>"' if punctuate else "") + "}]}\n\n"
        f"Chunks:\n{numbered}"
    )


def _words(text: str, limit: int) -> str:
    words = str(text or "").split()
    return " ".join(words[:limit])


def parse_labels(text: str, numbers: set[int]) -> tuple[list[Segment], int]:
    """Segments from the model's JSON. Out-of-range or overlapping ones are dropped and counted.

    Raises ValueError when the answer is not the expected JSON at all.
    """
    data = json.loads(text)
    raw = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("no 'segments' list")
    segments: list[Segment] = []
    taken: set[int] = set()
    invalid = 0
    for item in raw:
        try:
            start, end = int(item["from"]), int(item["to"])
            keep = bool(item.get("keep", True))
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        span = {n for n in range(start, end + 1) if n in numbers}
        if start > end or not span or span & taken:
            invalid += 1
            continue
        taken |= span
        kind = str(item.get("kind") or "lasting").strip().lower()
        segments.append(
            Segment(
                start=min(span),
                end=max(span),
                keep=keep,
                headline=_words(item.get("headline", ""), HEADLINE_MAX_WORDS) if keep else "",
                kind=kind if kind in KINDS else "lasting",
                note=_words(item.get("note", ""), NOTE_MAX_WORDS) if keep else "",
                text=str(item.get("text") or "").strip() if keep else "",
            )
        )
    return segments, invalid


_NORM_RE = re.compile(r"[^\w$%']+")


def _norm(word: str) -> str:
    return _NORM_RE.sub("", word.lower())


def apply_punctuation(original: str, punctuated: str) -> str:
    """The original words, every one and in order, with the punctuation and capitals the model gave
    them wherever the two line up. Words the model added are never used; words it dropped keep their
    original form. When too little lines up to be the same passage, the original is returned."""
    raw = original.split()
    pun = punctuated.split()
    a, b = [_norm(w) for w in raw], [_norm(w) for w in pun]
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if not raw or matcher.ratio() < PUNCTUATE_ALIGN_MIN:
        return original
    out = list(raw)
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out[i1 + k] = pun[j1 + k]  # same word, with the model's punctuation and capitals
    return " ".join(out)


def windows(chunks: list[Chunk], limit: int = WINDOW_WORDS) -> list[list[Chunk]]:
    """The chunks a request can carry, split for very long videos; numbering continues across."""
    out: list[list[Chunk]] = []
    current: list[Chunk] = []
    count = 0
    for chunk in chunks:
        size = len(chunk.text.split())
        if current and count + size > limit:
            out.append(current)
            current, count = [], 0
        current.append(chunk)
        count += size
    if current:
        out.append(current)
    return out


def request_body(prompt: str) -> dict[str, Any]:
    """A generateContent body for a label request (shared by direct and batch calls)."""
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": LABEL_MAX_OUTPUT_TOKENS,
        },
    }


# --- the labelling run -----------------------------------------------------------------------------


@dataclass
class VideoText:
    """One video ready to label: its transcript and the chunks cleanup kept for the model."""

    video_id: str
    title: str
    published: str
    transcript: str
    chunks: list[Chunk]


@dataclass
class LabelRun:
    labels: dict[str, VideoLabels] = field(default_factory=dict)
    waiting: list[str] = field(default_factory=list)  # videos left for the next run (quota, batch)
    notices: list[str] = field(default_factory=list)
    labelled_now: int = 0  # videos that cost a request this run (the rest came from the cache)


class Labeler:
    """Labels videos once: from the cache, else in a Gemini batch (half price), else directly.

    A project without billing cannot use batch mode; Google answers FAILED_PRECONDITION and the run
    uses standard calls, saying so. A used-up daily quota stops the run cleanly: what was labelled
    is kept and the rest waits for the next run. A batch still running after `batch_wait` seconds is
    remembered in `state_path`, and the next run collects it.
    """

    def __init__(
        self,
        gemini: Any,
        store: KnowledgeStore,
        today: str,
        *,
        state_path: Path,
        use_batch: bool = True,
        batch_wait: float = 1800.0,
        poll_every: float = 20.0,
        workers: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.gemini, self.store, self.today = gemini, store, today
        self.state_path = state_path
        self.use_batch = use_batch
        self.batch_wait, self.poll_every, self.workers = batch_wait, poll_every, workers
        self._sleep, self._clock = sleep, clock

    # -- public --

    def run(self, videos: list[VideoText], progress: Callable[[int, int], None] | None = None) -> LabelRun:
        out = LabelRun()
        pending: list[VideoText] = []
        for video in videos:
            cached = self.store.get(video.video_id, self._key(video))
            if cached is not None:
                out.labels[video.video_id] = cached
            elif not video.chunks:
                out.labels[video.video_id] = VideoLabels(video.video_id, self._key(video), self.gemini.model)
            else:
                pending.append(video)
        if not pending:
            return out
        answers: dict[str, str] = {}
        if self.use_batch:
            done = self._run_batch(pending, out)
            if done is None:  # still running at Google: collected next run
                out.waiting = [v.video_id for v in pending]
                return out
            answers.update(done)
        direct = [v for v in pending if not all(k in answers for k, _ in self._requests(v))]
        answers.update(self._run_direct(direct, out))
        for count, video in enumerate(pending, 1):
            keys = [k for k, _ in self._requests(video)]
            if not all(k in answers for k in keys):
                out.waiting.append(video.video_id)
                continue
            labels = self._assemble(video, [answers[k] for k in keys])
            if labels is None:
                out.waiting.append(video.video_id)
                continue
            self.store.put(labels)
            out.labels[video.video_id] = labels
            out.labelled_now += 1
            if progress:
                progress(count, len(pending))
        return out

    # -- requests --

    def _key(self, video: VideoText) -> str:
        return label_key(video.video_id, video.transcript, self.gemini.model)

    def _requests(self, video: VideoText, *, keep_when_unsure: bool = False) -> list[tuple[str, str]]:
        """(request key, prompt) for each window of the video. Unpunctuated captions come back
        punctuated too, in smaller windows, because the answer then carries the words."""
        punctuate = not is_punctuated(video.transcript)
        size = PUNCTUATE_WINDOW_WORDS if punctuate else WINDOW_WORDS
        return [
            (
                f"{self._key(video)}#{i}",  # tied to the transcript, so a changed one asks again
                label_prompt(
                    video.title,
                    video.published,
                    self.today,
                    window,
                    keep_when_unsure=keep_when_unsure,
                    punctuate=punctuate,
                ),
            )
            for i, window in enumerate(windows(video.chunks, size))
        ]

    def _assemble(
        self, video: VideoText, texts: list[str], *, relabelled: bool = False
    ) -> VideoLabels | None:
        numbers = {c.n for c in video.chunks}
        segments: list[Segment] = []
        invalid = 0
        for text in texts:
            try:
                found, bad = parse_labels(text, numbers)
            except (ValueError, TypeError) as exc:
                logger.info("Unusable labels for %s: %s", video.video_id, exc)
                return None
            segments += found
            invalid += bad
        labels = VideoLabels(
            video.video_id,
            self._key(video),
            self.gemini.model,
            segments,
            invalid,
            relabelled,
            sorted(numbers),
        )
        if not relabelled and labels.kept_share(sorted(numbers)) < LOW_KEEP_SHARE:
            retry = self._direct_texts([p for _k, p in self._requests(video, keep_when_unsure=True)])
            # A retry that failed (a quota, a bad answer) leaves the video waiting; the low-keep labels
            # are not saved as if they were final.
            return self._assemble(video, retry, relabelled=True) if retry is not None else None
        return labels

    # -- direct calls --

    def _call(self, prompt: str) -> str:
        result = self.gemini.generate(
            prompt,
            system=SYSTEM,
            json_mode=True,
            max_output_tokens=LABEL_MAX_OUTPUT_TOKENS,
            allow_fallback=False,
            timeout=LABEL_TIMEOUT,
        )
        return result.text

    def _direct_texts(self, prompts: list[str]) -> list[str] | None:
        try:
            return [self._call(p) for p in prompts]
        except GeminiError as exc:
            logger.info("Relabelling failed: %s", exc.detail)
            return None

    def _run_direct(self, videos: list[VideoText], out: LabelRun) -> dict[str, str]:
        answers: dict[str, str] = {}
        stop = threading.Event()
        failures: list[str] = []
        lock = threading.Lock()

        def one(item: tuple[str, str]) -> None:
            key, prompt = item
            if stop.is_set():
                return
            try:
                text = self._call(prompt)
            except GeminiError as exc:
                if exc.quota:
                    stop.set()
                with lock:
                    failures.append(exc.detail)
                return
            with lock:
                answers[key] = text

        items = [req for v in videos for req in self._requests(v)]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(one, items))
        if stop.is_set():
            out.notices.append(
                "Gemini's daily request quota is used up, so some videos wait for the next run "
                "(enable billing on the key's project to finish in one run)."
            )
        elif failures:
            out.notices.append(
                f"{len(failures)} labelling requests failed ({failures[0]}); they are retried next run."
            )
        return answers

    # -- batch --

    def _run_batch(self, videos: list[VideoText], out: LabelRun) -> dict[str, str] | None:
        """Answers by request key, {} when batch mode cannot be used, None when still running."""
        requests = {key: request_body(prompt) for v in videos for key, prompt in self._requests(v)}
        state = self._load_state()
        job = state.get("job") if state.get("model") == self.gemini.model else None
        if job and not set(state.get("keys", [])) <= set(requests):
            # A job for other videos or changed transcripts: its answers cannot be used. Said once.
            out.notices.append(
                "An earlier Gemini batch was for other videos or older transcripts; a new one was started."
            )
            job = None
        if not job:
            try:
                keys = list(requests)
                job = self.gemini.submit_batch([(k, requests[k]) for k in keys], "notebook-alchemy labels")
            except GeminiError as exc:
                if self.gemini.batch_refused(exc):
                    out.notices.append(
                        "Gemini batch mode (half price) needs billing on the key's Google Cloud project, "
                        "so standard calls were used."
                    )
                else:
                    out.notices.append(f"Gemini batch mode failed ({exc.detail}); standard calls were used.")
                return {}
            state = {"job": job, "keys": keys, "model": self.gemini.model}
            self._save_state(state)
        deadline = self._clock() + self.batch_wait
        while True:
            try:
                status, results = self.gemini.batch_results(job, state["keys"])
            except GeminiError as exc:
                out.notices.append(
                    f"Could not read the Gemini batch ({exc.detail}); it is checked again next run."
                )
                return None
            if results is not None:
                self._save_state({})
                return {k: r.text for k, r in results.items() if isinstance(r, GeminiResult)}
            if any(s in status for s in ("FAILED", "CANCELLED", "EXPIRED")):
                self._save_state({})
                out.notices.append(f"The Gemini batch ended as {status}; standard calls were used.")
                return {}
            if self._clock() >= deadline:
                out.notices.append(
                    "Gemini is still working on the batch; run the same command again to continue."
                )
                return None
            self._sleep(self.poll_every)

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable batch state %s: %s", self.state_path, exc)
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save batch state %s: %s", self.state_path, exc)
