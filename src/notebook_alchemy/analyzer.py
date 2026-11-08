"""Source analysis: discovery, domain clustering, and token/cost estimates."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .channel import DEFAULT_TABS, ChannelScanner, video_id_from_url
from .client import NotebookLMClient
from .config import DEFAULT_MAX_PER_VOLUME, AlchemyConfig, is_usable_api_key
from .errors import GeminiError, NotebookLMError, SessionError
from .gemini import GeminiClient, price_for
from .mock_data import MOCK_NOTEBOOK_ID, MOCK_NOTEBOOK_TITLE, get_mock_sources

logger = logging.getLogger(__name__)

# (stage label, items done, items total)
ProgressFn = Callable[[str, int, int], None]

# Estimates. A spoken video is about 2.5 words per second, about 1.33 tokens per word.
TOKENS_PER_SPOKEN_SECOND = 3.3
AVG_TRANSCRIPT_TOKENS_PER_VIDEO = 4500  # used when a source reports no duration
OUTPUT_TOKENS_PER_VIDEO = 500

MAX_DOMAIN_SAMPLE = 300
ASSIGN_BATCH_SIZE = 150
MIN_DOMAINS, MAX_DOMAINS = 3, 15
MISC_DOMAIN = "Miscellaneous"
NO_KEY_NOTE = "No Gemini API key set; used keyword matching."


def estimate_video_tokens(duration_seconds: int) -> int:
    """Transcript tokens for one video, from its duration when known."""
    if duration_seconds > 0:
        return max(200, int(duration_seconds * TOKENS_PER_SPOKEN_SECOND))
    return AVG_TRANSCRIPT_TOKENS_PER_VIDEO


@dataclass
class VideoSourceSummary:
    """A video source found in a channel, playlist or notebook."""

    source_id: str
    title: str
    url: str
    status: str
    duration_seconds: int = 0

    @property
    def estimated_tokens(self) -> int:
        return estimate_video_tokens(self.duration_seconds)


@dataclass
class DryRunReport:
    """Everything known about a source before any synthesis runs."""

    notebook_id: str
    notebook_title: str
    total_sources: int
    youtube_videos: list[VideoSourceSummary]
    other_sources: list[dict[str, Any]]
    total_estimated_tokens: int
    estimated_input_cost_usd: float
    estimated_synthesis_output_cost_usd: float
    domain_videos: dict[str, list[VideoSourceSummary]] = field(default_factory=dict)
    is_mock: bool = False
    origin: str = "youtube"  # youtube | notebooklm | mock
    cluster_method: str = "keyword"  # gemini | keyword
    model: str = ""
    notes: list[str] = field(default_factory=list)
    split_requests: int = 0  # Gemini requests spent splitting large domains into subtopics

    @property
    def total_estimated_cost_usd(self) -> float:
        return self.estimated_input_cost_usd + self.estimated_synthesis_output_cost_usd

    @property
    def estimated_gemini_requests(self) -> int:
        """Domain discovery, title assignment batches, subtopic splits, one synthesis call per volume."""
        titles = len(self.youtube_videos)
        volumes = sum(-(-len(v) // DEFAULT_MAX_PER_VOLUME) for v in self.domain_videos.values())
        return 1 + -(-titles // ASSIGN_BATCH_SIZE) + self.split_requests + volumes


_WORD_RE = re.compile(r"[a-z][a-z0-9.'-]{3,}")
# Words too common in video titles to name a subtopic.
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


def _kw(pattern: str) -> re.Pattern[str]:
    """Keyword pattern that must start at a word boundary (so 'design' skips 'Redesign')."""
    return re.compile(r"(?<![a-z0-9])(?:" + pattern + ")", re.IGNORECASE)


# Keyword fallback taxonomy. First match wins. Used when Gemini is off or unavailable.
KEYWORD_DOMAINS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Lead Generation & Client Acquisition",
        _kw(
            r"lead\b|leads\b|clients?\b(?!-side)|acqui|attract|cold\b|prospect|outreach|marketing|funnel|sales\b"
        ),
    ),
    (
        "Client Retention, Onboarding & QBRs",
        _kw(r"qbr|quarterly|retain|churn|past client|onboard|deliver"),
    ),
    (
        "Pricing & Proposal Strategy",
        _kw(r"pric|rates?\b|proposal|pitch|charge|money|estimate|dollar|retainer|value-based"),
    ),
    (
        "SEO & Local Search",
        _kw(r"seo\b|gbp\b|google business|rank|local\b|map pack|citation|schema"),
    ),
    (
        "Solo Operator Systems & Productivity",
        _kw(r"productivity|time\b|system|scale\b|crm\b|tools?\b|workflow|routine|homeschool|burnout"),
    ),
    (
        "AI, LLMs & Agentic Coding",
        _kw(
            r"ai\b|llms?\b|gpt|agent|vibe cod|claude|chatgpt|openai|gemini|cursor|copilot|model\b|neural"
            r"|deep learning|machine learning"
        ),
    ),
    (
        "Frontend & Modern UI Engineering",
        _kw(
            r"react|vue|angular|svelte|next\.?js|nuxt|html|css|tailwind|javascript|typescript|vanilla js"
            r"|ui\b|animation|dom\b|flexbox|grid\b|bootstrap|svg|canvas|frontend|hover effect|webgl"
        ),
    ),
    (
        "Backend, APIs & Distributed Systems",
        _kw(
            r"node\b|nodejs|express\b|fastapi|django|flask|nest\b|rest\b|restful|graphql|api\b|apis\b"
            r"|backend|microservice|auth\b|oauth|jwt|server\b|strapi|postman"
        ),
    ),
    (
        "Databases & Data Storage",
        _kw(r"sql|postgres|mysql|mongo|database|prisma|redis|supabase|firebase|orm\b|couchdb"),
    ),
    (
        "DevOps, Cloud & Developer Tooling",
        _kw(
            r"docker|kubernetes|aws\b|cloud|ci/cd|git\b|github|linux|deploy|serverless|devops|nginx"
            r"|terminal|bash|powershell|vscode|vim\b|webpack|vite\b|npm\b"
        ),
    ),
    (
        "Programming Languages & Runtimes",
        _kw(
            r"python|rust\b|golang|go lang|java\b|c\+\+|c#|\.net|php|laravel|ruby|dart|flutter|swift\b"
            r"|kotlin|c programming"
        ),
    ),
    (
        "Full-Stack Projects & Crash Courses",
        _kw(
            r"crash course|full stack|build a\b|project|beginner|mastery|guide\b|from scratch|tutorial"
            r"|series\b|walkthrough"
        ),
    ),
    (
        "Mobile & Cross-Platform Development",
        _kw(r"mobile|react native|flutter|ionic|cordova|android|ios\b|xamarin|app dev"),
    ),
    (
        "Web Design & Agency Operations",
        _kw(r"website|wordpress|webflow|design\b|agency|freelanc|portfolio|wireframe"),
    ),
]
OTHER_DOMAIN = "General / Specialized Topics"


class NotebookAnalyzer:
    """Discovers sources, clusters them into domains, and estimates token cost."""

    split_requests = 0
    max_per_section = DEFAULT_MAX_PER_VOLUME

    def __init__(
        self,
        config: AlchemyConfig | None = None,
        *,
        api_key: str | None = None,
        force_offline: bool = False,
        client: NotebookLMClient | None = None,
        gemini: GeminiClient | None = None,
    ) -> None:
        self.config = config or AlchemyConfig.from_env()
        self.api_key = self.config.gemini_api_key if api_key is None else api_key
        self.model = self.config.gemini_model
        self.force_offline = force_offline
        self.client = client or NotebookLMClient(self.config)
        self._gemini = gemini
        self.cluster_method = "keyword"
        self.notes: list[str] = []

    @property
    def llm_enabled(self) -> bool:
        return is_usable_api_key(self.api_key) and not self.force_offline

    def _gemini_notices(self) -> list[str]:
        """Fallback-model notices from the Gemini client, if one was created."""
        return self._gemini.drain_notices() if self._gemini else []

    def _gemini_client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient(
                self.api_key,
                self.model,
                fallback_model=self.config.gemini_fallback_model,
                timeout=60.0,
            )
        return self._gemini

    # -- discovery --

    def generate_dry_run(
        self,
        notebook_query: str,
        *,
        use_mock: bool = False,
        limit: int | None = None,
        tabs: tuple[str, ...] = DEFAULT_TABS,
        max_per_section: int = DEFAULT_MAX_PER_VOLUME,
        progress: ProgressFn | None = None,
    ) -> DryRunReport:
        """Analyse a YouTube URL, an existing notebook, or (only with use_mock) the sample catalog."""
        self.max_per_section = max_per_section
        scanner = ChannelScanner()
        if not use_mock and scanner.is_youtube_url(notebook_query):
            return self.scan_channel(notebook_query, limit=limit, tabs=tabs, progress=progress)

        if use_mock:
            sources = get_mock_sources()[:limit] if limit else get_mock_sources()
            return self._build_report(
                nb_id=MOCK_NOTEBOOK_ID,
                nb_title=MOCK_NOTEBOOK_TITLE,
                videos=self._summaries_from_sources(sources)[0],
                others=[],
                total_sources=len(sources),
                origin="mock",
                progress=progress,
            )

        if not self.client.is_authenticated():
            raise SessionError(
                f"'{notebook_query}' is not a YouTube URL, and there is no NotebookLM session to look "
                "it up as a notebook.",
                hint="Pass a YouTube channel, playlist or video URL, or run `notebook-alchemy login`.",
            )
        nb = self.client.find_notebook(notebook_query)
        if not nb:
            raise NotebookLMError(
                f"No notebook matches '{notebook_query}'.",
                hint="Pass a YouTube URL, an exact notebook title or id, or use --mock for the sample.",
            )
        sources_raw = self.client.list_sources(str(nb.get("id", "")))
        if limit:
            sources_raw = sources_raw[:limit]
        videos, others = self._summaries_from_sources(sources_raw)
        return self._build_report(
            nb_id=str(nb.get("id", "")),
            nb_title=str(nb.get("title", notebook_query)),
            videos=videos,
            others=others,
            total_sources=len(sources_raw),
            origin="notebooklm",
            progress=progress,
        )

    def scan_channel(
        self,
        url: str,
        limit: int | None = None,
        *,
        tabs: tuple[str, ...] = DEFAULT_TABS,
        progress: ProgressFn | None = None,
    ) -> DryRunReport:
        """Scan a YouTube channel, playlist or video. Raises ChannelScanError on failure."""
        scan = ChannelScanner().scan(url, limit=limit, tabs=tabs)
        videos = [
            VideoSourceSummary(
                source_id=v.id,
                title=v.title,
                url=v.url,
                status="ready",
                duration_seconds=v.duration_seconds,
            )
            for v in scan.videos
        ]
        safe_id = re.sub(r"[^\w-]", "", scan.channel_name).lower() or "source"
        return self._build_report(
            nb_id=f"nb_{safe_id}",
            nb_title=scan.channel_name,
            videos=videos,
            others=[],
            total_sources=len(videos),
            origin="youtube",
            progress=progress,
        )

    @staticmethod
    def _summaries_from_sources(
        sources_raw: list[dict[str, Any]],
    ) -> tuple[list[VideoSourceSummary], list[dict[str, Any]]]:
        videos: list[VideoSourceSummary] = []
        others: list[dict[str, Any]] = []
        for s in sources_raw:
            url = str(s.get("url") or "")
            stype = str(s.get("type") or s.get("source_type") or "").lower()
            # notebooklm-py reports YouTube sources as type "youtube"; other video types (a Drive
            # video, an upload) have no captions to fetch, so only a YouTube URL counts otherwise.
            youtube_id = video_id_from_url(url) if url else ""
            is_youtube = stype == "youtube" or (bool(url) and not youtube_id.startswith("url_"))
            if is_youtube:
                videos.append(
                    VideoSourceSummary(
                        # The YouTube id, so transcripts can be fetched; NotebookLM's own source
                        # id is not a video id.
                        source_id=youtube_id
                        if youtube_id and not youtube_id.startswith("url_")
                        else str(s.get("id") or s.get("source_id") or ""),
                        title=str(s.get("title") or "Untitled Source"),
                        url=url,
                        status=str(s.get("status") or "ready"),
                    )
                )
            else:
                others.append(s)
        return videos, others

    def _build_report(
        self,
        *,
        nb_id: str,
        nb_title: str,
        videos: list[VideoSourceSummary],
        others: list[dict[str, Any]],
        total_sources: int,
        origin: str,
        progress: ProgressFn | None,
    ) -> DryRunReport:
        total_tokens = sum(v.estimated_tokens for v in videos)
        price_in, price_out, price_note = price_for(self.model)
        input_cost = (total_tokens / 1_000_000) * price_in
        output_cost = (len(videos) * OUTPUT_TOKENS_PER_VIDEO / 1_000_000) * price_out

        titles = [v.title for v in videos]
        indices = self._cluster_indices(titles, progress)
        indices = self._split_large_domains(titles, indices, progress)
        domain_videos = {d: [videos[i] for i in ids] for d, ids in indices.items()}
        notes = list(self.notes)
        if price_note:
            notes.append(price_note)
        return DryRunReport(
            notebook_id=nb_id,
            notebook_title=nb_title,
            total_sources=total_sources,
            youtube_videos=videos,
            other_sources=others,
            total_estimated_tokens=total_tokens,
            estimated_input_cost_usd=round(input_cost, 4),
            estimated_synthesis_output_cost_usd=round(output_cost, 4),
            domain_videos=domain_videos,
            is_mock=origin == "mock",
            origin=origin,
            cluster_method=self.cluster_method,
            model=self.model,
            notes=notes,
            split_requests=self.split_requests,
        )

    # -- clustering --

    def cluster_titles(self, titles: list[str], progress: ProgressFn | None = None) -> dict[str, list[str]]:
        """Group titles into domains. Every title appears in exactly one domain."""
        indices = self._cluster_indices(titles, progress)
        return {d: [titles[i] for i in ids] for d, ids in indices.items()}

    def _cluster_indices(self, titles: list[str], progress: ProgressFn | None = None) -> dict[str, list[int]]:
        self.notes = []
        self.cluster_method = "keyword"
        if not titles:
            return {}
        if self.llm_enabled:
            try:
                clusters = self._cluster_with_gemini(titles, progress)
                self.cluster_method = "gemini"
                self.notes.extend(self._gemini_notices())
                return clusters
            except (GeminiError, ValueError, KeyError, TypeError) as exc:
                self.notes.extend(self._gemini_notices())
                reason = exc.message if isinstance(exc, GeminiError) else f"unusable answer ({exc})"
                self.notes.append(f"Gemini clustering unavailable: {reason} Used keyword matching.")
        elif not self.force_offline:
            self.notes.append(NO_KEY_NOTE)
        return self._cluster_with_keywords(titles)

    @staticmethod
    def _sample(titles: list[str], size: int) -> list[str]:
        if len(titles) <= size:
            return titles
        step = len(titles) / size
        return [titles[int(i * step)] for i in range(size)]

    def _discover_domains(self, titles: list[str]) -> list[str]:
        sample = "\n".join(f"- {t[:120]}" for t in self._sample(titles, MAX_DOMAIN_SAMPLE))
        prompt = (
            "You organise a video archive. The titles below are a sample of it.\n"
            f"Name {MIN_DOMAINS + 3} to 12 distinct subject domains that together cover the archive. "
            "Each name is 2 to 6 words, specific to this archive's subject, and not a generic label.\n"
            'Return only JSON: {"domains": ["Domain name", ...]}\n\n'
            f"Titles:\n{sample}"
        )
        result = self._gemini_client().generate(
            prompt, json_mode=True, max_output_tokens=4096, temperature=0.1, timeout=45.0
        )
        raw = json.loads(result.text).get("domains")
        if not isinstance(raw, list):
            raise TypeError("no 'domains' list")
        names: list[str] = []
        seen: set[str] = set()
        for item in raw:
            name = " ".join(str(item.get("name", "") if isinstance(item, dict) else item).split())
            if name and name.lower() not in seen and name.lower() != MISC_DOMAIN.lower():
                seen.add(name.lower())
                names.append(name[:80])
        if len(names) < MIN_DOMAINS:
            raise ValueError(f"only {len(names)} usable domains")
        return names[:MAX_DOMAINS]

    def _cluster_with_gemini(self, titles: list[str], progress: ProgressFn | None) -> dict[str, list[int]]:
        """Two stages: discover domains from a sample, then assign every title by index."""
        domains = self._discover_domains(titles)
        listing = "\n".join(f"{i}: {name}" for i, name in enumerate(domains))
        assigned: dict[int, int] = {}
        batches = range(0, len(titles), ASSIGN_BATCH_SIZE)
        for done, start in enumerate(batches, 1):
            batch = titles[start : start + ASSIGN_BATCH_SIZE]
            lines = "\n".join(f"{start + i}: {t[:120]}" for i, t in enumerate(batch))
            prompt = (
                "Assign each numbered video title to exactly one domain.\n"
                f"Domains:\n{listing}\n\nTitles:\n{lines}\n\n"
                'Return only JSON: {"assignments": {"<title number>": <domain number>, ...}} '
                "covering every title number."
            )
            result = self._gemini_client().generate(
                prompt, json_mode=True, max_output_tokens=16384, temperature=0.0, timeout=60.0
            )
            mapping = json.loads(result.text).get("assignments")
            if not isinstance(mapping, dict):
                raise TypeError("no 'assignments' object")
            for key, value in mapping.items():
                try:
                    t_idx, d_idx = int(key), int(value)
                except (TypeError, ValueError):
                    continue
                if start <= t_idx < start + len(batch) and 0 <= d_idx < len(domains):
                    assigned[t_idx] = d_idx
            if progress:
                progress("Sorting videos into topics", done, len(batches))

        clusters: dict[str, list[int]] = {name: [] for name in domains}
        misc: list[int] = []
        for i in range(len(titles)):
            if i in assigned:
                clusters[domains[assigned[i]]].append(i)
            else:
                misc.append(i)
        if misc:
            self.notes.append(
                f"{len(misc)} of {len(titles)} titles had no valid assignment and were placed "
                f"in '{MISC_DOMAIN}'."
            )
            clusters[MISC_DOMAIN] = misc
        return {d: ids for d, ids in clusters.items() if ids}

    # -- subtopics --

    def _split_large_domains(
        self, titles: list[str], indices: dict[str, list[int]], progress: ProgressFn | None
    ) -> dict[str, list[int]]:
        """Split each domain larger than one volume into named subtopics ("Domain: Subtopic").

        A volume called "Vol 7" tells a reader nothing; a subtopic name does. With Gemini the
        subtopics are discovered from that domain's own titles and every title is assigned by
        number; otherwise titles are grouped by the words that set them apart. A subtopic still
        larger than one volume is cut into parts by the engine.
        """
        self.split_requests = 0
        limit = max(1, getattr(self, "max_per_section", DEFAULT_MAX_PER_VOLUME))
        out: dict[str, list[int]] = {}
        for domain, ids in indices.items():
            if len(ids) <= limit:
                out[domain] = ids
                continue
            groups: dict[str, list[int]] | None = None
            if self.cluster_method == "gemini" and self.llm_enabled:
                try:
                    groups = self._subtopics_with_gemini(domain, [titles[i] for i in ids], limit, progress)
                except (GeminiError, ValueError, KeyError, TypeError) as exc:
                    reason = exc.message if isinstance(exc, GeminiError) else f"unusable answer ({exc})"
                    self.notes.append(f"Could not split '{domain}' with Gemini ({reason}); used title words.")
                self.notes.extend(self._gemini_notices())
            if groups is None:
                groups = self._subtopics_by_words(domain, [titles[i] for i in ids], limit)
            for name, local in groups.items():
                if local:
                    out.setdefault(f"{domain}: {name}", []).extend(ids[j] for j in local)
        return out

    def _subtopics_with_gemini(
        self, domain: str, titles: list[str], limit: int, progress: ProgressFn | None
    ) -> dict[str, list[int]]:
        wanted = -(-len(titles) // limit)
        sample = "\n".join(f"- {t[:120]}" for t in self._sample(titles, MAX_DOMAIN_SAMPLE))
        prompt = (
            f"These video titles all belong to the subject '{domain}'. Name {wanted} to {wanted + 1} "
            "distinct subtopics within it that together cover the titles. Each name is 2 to 6 words, "
            f"specific, and does not repeat the words '{domain}'.\n"
            'Return only JSON: {"subtopics": ["Subtopic name", ...]}\n\n'
            f"Titles:\n{sample}"
        )
        result = self._gemini_client().generate(
            prompt, json_mode=True, max_output_tokens=4096, temperature=0.1, timeout=45.0
        )
        self.split_requests += 1
        raw = json.loads(result.text).get("subtopics")
        if not isinstance(raw, list):
            raise TypeError("no 'subtopics' list")
        names: list[str] = []
        seen: set[str] = set()
        for item in raw:
            name = " ".join(str(item).split())[:60]
            if name and name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
        if len(names) < 2:
            raise ValueError(f"only {len(names)} usable subtopics")
        assigned = self._assign_by_number(titles, names, f"Dividing {domain}", progress)
        groups: dict[str, list[int]] = {name: [] for name in names}
        other: list[int] = []
        for i in range(len(titles)):
            (groups[names[assigned[i]]] if i in assigned else other).append(i)
        if other:
            groups["Other"] = other
        return groups

    def _assign_by_number(
        self, titles: list[str], names: list[str], label: str, progress: ProgressFn | None
    ) -> dict[int, int]:
        """Ask Gemini to place each numbered title under one numbered name; invalid answers are dropped."""
        listing = "\n".join(f"{i}: {name}" for i, name in enumerate(names))
        assigned: dict[int, int] = {}
        batches = range(0, len(titles), ASSIGN_BATCH_SIZE)
        for done, start in enumerate(batches, 1):
            batch = titles[start : start + ASSIGN_BATCH_SIZE]
            lines = "\n".join(f"{start + i}: {t[:120]}" for i, t in enumerate(batch))
            prompt = (
                "Assign each numbered video title to exactly one category.\n"
                f"Categories:\n{listing}\n\nTitles:\n{lines}\n\n"
                'Return only JSON: {"assignments": {"<title number>": <category number>, ...}} '
                "covering every title number."
            )
            result = self._gemini_client().generate(
                prompt, json_mode=True, max_output_tokens=16384, temperature=0.0, timeout=60.0
            )
            self.split_requests += 1  # only subtopic splitting assigns by number
            mapping = json.loads(result.text).get("assignments")
            if not isinstance(mapping, dict):
                raise TypeError("no 'assignments' object")
            for key, value in mapping.items():
                try:
                    t_idx, n_idx = int(key), int(value)
                except (TypeError, ValueError):
                    continue
                if start <= t_idx < start + len(batch) and 0 <= n_idx < len(names):
                    assigned[t_idx] = n_idx
            if progress:
                progress(label, done, len(batches))
        return assigned

    @staticmethod
    def _subtopics_by_words(domain: str, titles: list[str], limit: int) -> dict[str, list[int]]:
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

    @staticmethod
    def _cluster_with_keywords(titles: list[str]) -> dict[str, list[int]]:
        clusters: dict[str, list[int]] = {name: [] for name, _ in KEYWORD_DOMAINS}
        clusters[OTHER_DOMAIN] = []
        for i, title in enumerate(titles):
            for name, pattern in KEYWORD_DOMAINS:
                if pattern.search(title):
                    clusters[name].append(i)
                    break
            else:
                clusters[OTHER_DOMAIN].append(i)
        return {d: ids for d, ids in clusters.items() if ids}
