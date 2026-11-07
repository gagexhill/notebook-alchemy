"""Domain compendium generation: live Gemini synthesis, or an honest offline index."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .channel import TranscriptManager, TranscriptResult
from .config import DEFAULT_GEMINI_MODEL, is_usable_api_key
from .errors import GeminiError
from .gemini import GeminiClient

logger = logging.getLogger(__name__)

SECTION_RELATED = "## Related Domains"
# Prompt budget in characters (about 4 per token): well inside a 1M-token context window.
MAX_PROMPT_CHARS = 1_600_000
MAX_CHARS_PER_VIDEO = 60_000
LIVE_MAX_OUTPUT_TOKENS = 32_768
LIVE_TIMEOUT_SECONDS = 150.0
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
# Files the vault writes beside the compendiums; a domain may not take these names.
INDEX_FILENAME = "INDEX.md"
CATALOG_FILENAME = "MASTER_SOURCES_CATALOG.md"
_RESERVED_STEMS = {INDEX_FILENAME[:-3].lower(), CATALOG_FILENAME[:-3].lower()}
# Volume 2+ files end in _Vol_<n>; a domain slug of that form could take another domain's file.
_VOLUME_FORM = re.compile(r"_vol_\d+$", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are a Principal Knowledge Architect. Synthesize raw video transcripts into a dense, "
    "authoritative domain compendium for Google NotebookLM. No filler, no preamble, no sponsor "
    "reads. Attribute every claim to its source video by number, like [3]."
)

ProgressFn = Callable[[str, int, int], None]


def yaml_str(value: object) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(str(value), ensure_ascii=False)


def safe_filename(text: str) -> str:
    """Filesystem-safe stem: never empty, never a Windows reserved name, at most 80 characters."""
    cleaned = re.sub(r"[^\w\s-]", "", text).strip()
    stem = re.sub(r"[-\s]+", "_", cleaned).strip("_")[:80].strip("_")
    if not stem:
        stem = "domain"
    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"{stem}_"
    return stem


def unique_slugs(domains: list[str]) -> dict[str, str]:
    """One distinct slug per domain; later collisions get a short stable hash suffix.

    A slug that would match the vault's own files (INDEX, MASTER_SOURCES_CATALOG, compared without
    case for case-insensitive file systems) or the `_Vol_<n>` form of a volume file also gets the
    suffix, so no domain can overwrite another file.
    """
    slugs: dict[str, str] = {}
    taken: set[str] = set(_RESERVED_STEMS)
    for domain in domains:
        base = safe_filename(domain)
        slug = base
        if slug.lower() in taken or _VOLUME_FORM.search(slug):
            slug = f"{base[:72]}_{hashlib.sha256(domain.encode('utf-8')).hexdigest()[:6]}"
        taken.add(slug.lower())
        slugs[domain] = slug
    return slugs


def notebook_url(notebook_id: str) -> str:
    """A NotebookLM link only for a real notebook id; synthetic ids get no link."""
    return f"https://notebooklm.google.com/notebook/{notebook_id}" if _UUID_RE.match(notebook_id) else ""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class DomainCompendium:
    """One generated compendium volume."""

    domain: str
    volume: int
    titles: list[str]
    source_ids: list[str]
    relative_path: str
    content: str
    token_count: int
    is_live_synthesized: bool = False
    warnings: list[str] = field(default_factory=list)


class Synthesizer:
    """Builds compendiums from clustered videos."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_GEMINI_MODEL,
        *,
        fallback_model: str | None = None,
        gemini: GeminiClient | None = None,
        transcripts: TranscriptManager | None = None,
    ) -> None:
        self.api_key = api_key or ""
        self.model = model
        self.fallback_model = fallback_model
        self._gemini = gemini
        self._transcripts = transcripts

    def _is_valid_api_key(self) -> bool:
        return is_usable_api_key(self.api_key)

    def _safe_filename(self, text: str) -> str:
        return safe_filename(text)

    def _client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient(self.api_key, self.model, fallback_model=self.fallback_model)
        return self._gemini

    def _transcript_manager(self) -> TranscriptManager:
        if self._transcripts is None:
            self._transcripts = TranscriptManager()
        return self._transcripts

    # -- compendium --

    def synthesize_cluster(
        self,
        domain: str,
        videos: list[dict[str, Any]],
        volume: int = 1,
        *,
        force_offline: bool = False,
        notebook_title: str = "Untitled",
        notebook_id: str = "",
        slug: str | None = None,
        related_domains: list[tuple[str, str]] | None = None,
        progress: ProgressFn | None = None,
    ) -> DomainCompendium:
        """Return one compendium. Live synthesis falls back to the offline index with a warning."""
        titles = [str(v.get("title") or "Untitled") for v in videos]
        source_ids = [str(v.get("id") or v.get("source_id") or f"src_{i}") for i, v in enumerate(videos)]
        vol_suffix = f"_Vol_{volume}" if volume > 1 else ""
        rel_path = f"compendiums/{slug or safe_filename(domain)}{vol_suffix}.md"

        warnings: list[str] = []
        content = ""
        is_live = False
        if not force_offline and self._is_valid_api_key():
            try:
                content = self._synthesize_live(
                    domain, videos, volume, notebook_title, notebook_id, warnings, progress
                )
                is_live = True
            except GeminiError as exc:
                if self._gemini is not None:
                    warnings.extend(f"{domain} (Vol. {volume}): {n}" for n in self._gemini.drain_notices())
                warnings.append(
                    f"{domain} (Vol. {volume}): live synthesis failed ({exc.message}); "
                    "wrote the offline index instead."
                )
        if not is_live:
            content = self._offline_compendium(
                domain, videos, volume, notebook_title, notebook_id, related_domains or []
            )

        return DomainCompendium(
            domain=domain,
            volume=volume,
            titles=titles,
            source_ids=source_ids,
            relative_path=rel_path,
            content=content,
            token_count=max(len(content) // 4, 1),
            is_live_synthesized=is_live,
            warnings=warnings,
        )

    def _frontmatter(
        self,
        domain: str,
        volume: int,
        count: int,
        notebook_title: str,
        notebook_id: str,
        mode: str,
    ) -> str:
        vol_label = f" (Vol. {volume})" if volume > 1 else ""
        tag = safe_filename(domain).lower().replace("_", "-")
        lines = [
            "---",
            f"title: {yaml_str(f'{domain}{vol_label} - Domain Compendium')}",
            f"domain: {yaml_str(domain)}",
            f"volume: {volume}",
            f"notebook_title: {yaml_str(notebook_title)}",
        ]
        if notebook_url(notebook_id):  # only a real notebook id; derived ids are not recorded
            lines.append(f"notebook_id: {yaml_str(notebook_id)}")
        if notebook_id.startswith("mock_"):
            lines.append("sample_data: true")
        lines += [
            f"source_count: {count}",
            f"synthesis: {yaml_str(mode)}",
            f"generated_at: {yaml_str(_now())}",
            f"generator: {yaml_str(f'Notebook Alchemy v{__version__}')}",
            "tags:",
            "  - notebooklm",
            "  - domain-compendium",
            f"  - {tag}",
            "---",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _source_table(videos: list[dict[str, Any]], status: dict[str, str] | None = None) -> str:
        rows = ["| # | Title | Source ID | Transcript |", "| :-: | :-- | :-- | :-- |"]
        for i, v in enumerate(videos, 1):
            vid = str(v.get("id") or v.get("source_id") or f"src_{i}")
            title = str(v.get("title") or f"Video {i}").replace("|", "-")
            url = str(v.get("url") or "")
            link = f"[{title}]({url})" if url else title
            rows.append(f"| {i} | {link} | `{vid}` | {(status or {}).get(vid, 'n/a')} |")
        return "\n".join(rows)

    def _synthesize_live(
        self,
        domain: str,
        videos: list[dict[str, Any]],
        volume: int,
        notebook_title: str,
        notebook_id: str,
        warnings: list[str],
        progress: ProgressFn | None,
    ) -> str:
        label = f"{domain} (Vol. {volume})"
        items = [(str(v.get("id") or v.get("source_id") or ""), str(v.get("title") or "")) for v in videos]
        results: dict[str, TranscriptResult] = self._transcript_manager().fetch_many(
            items,
            progress=(lambda d, t: progress(f"Reading transcripts: {label}", d, t)) if progress else None,
        )

        per_video = max(1_000, min(MAX_CHARS_PER_VIDEO, MAX_PROMPT_CHARS // max(len(videos), 1)))
        blocks: list[str] = []
        status: dict[str, str] = {}
        shortened = 0
        for n, v in enumerate(videos, 1):
            vid = str(v.get("id") or v.get("source_id") or "")
            res = results.get(vid) or TranscriptResult(vid)
            text = res.text
            if text:
                status[vid] = f"{res.source}" + (" (shortened)" if len(text) > per_video else "")
                if len(text) > per_video:
                    shortened += 1
                    text = text[:per_video]
                body = text
            else:
                status[vid] = f"none ({res.detail or res.source})"
                body = "(No transcript available. Use only the title; do not invent content.)"
            blocks.append(f"### [{n}] {v.get('title') or 'Untitled'}\n{body}\n")

        no_text = sum(1 for s in status.values() if s.startswith("none"))
        if no_text:
            warnings.append(f"{label}: {no_text} of {len(videos)} videos had no transcript (title only).")
        if shortened:
            warnings.append(f"{label}: {shortened} transcripts were shortened to {per_video:,} characters.")
        blocked = [r for r in results.values() if r.source == "blocked"]
        if blocked:
            noun = "request" if len(blocked) == 1 else "requests"
            warnings.append(
                f"{label}: YouTube blocked {len(blocked)} transcript {noun}; retry later or add "
                "local transcript files."
            )

        prompt = (
            f"Synthesize the {len(videos)} numbered videos below, from '{notebook_title}', into one "
            f"Markdown compendium for the domain '{domain}' (volume {volume}).\n\n"
            "Rules:\n"
            "1. Do not write YAML frontmatter, a title line, or a source table; those are added for you.\n"
            "2. Use these sections: ## Executive Summary, ## Key Concepts and Mental Models, "
            "## Procedures and Frameworks, ## Common Pitfalls.\n"
            "3. Extract concrete steps, frameworks and examples the speakers actually give. "
            "Cite the source video number after each claim, like [3].\n"
            "4. Remove filler, sponsor reads and intros. Do not invent facts that are not in a "
            "transcript.\n\n"
            "Sources:\n\n" + "\n".join(blocks)
        )
        result = self._client().generate(
            prompt,
            system=SYSTEM_PROMPT,
            max_output_tokens=LIVE_MAX_OUTPUT_TOKENS,
            temperature=0.2,
            timeout=LIVE_TIMEOUT_SECONDS,
        )
        warnings.extend(f"{label}: {n}" for n in self._client().drain_notices())
        if result.truncated:
            warnings.append(
                f"{label}: Gemini stopped at its output limit, so the synthesis may end early. "
                "Lower --max-per-vol to give it less to cover."
            )
        body = result.text.strip()
        fenced = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```$", body, re.DOTALL)
        if fenced:
            body = fenced.group(1).strip()

        vol_label = f" (Vol. {volume})" if volume > 1 else ""
        link = notebook_url(notebook_id)
        header = f"# {domain}{vol_label} — Domain Compendium\n\n"
        if link:
            header += f"> **Source Notebook:** [{notebook_title}]({link}) | **Sources:** {len(videos)}\n\n"
        return (
            self._frontmatter(
                domain,
                volume,
                len(videos),
                notebook_title,
                notebook_id,
                f"gemini:{result.model}",
            )
            + header
            + body
            + "\n\n## Sources\n\n"
            + self._source_table(videos, status)
            + "\n"
        )

    def _offline_compendium(
        self,
        domain: str,
        videos: list[dict[str, Any]],
        volume: int,
        notebook_title: str,
        notebook_id: str,
        related: list[tuple[str, str]],
    ) -> str:
        """A title-only index. It states plainly that nothing was summarized."""
        vol_label = f" (Vol. {volume})" if volume > 1 else ""
        sample = "; ".join(str(v.get("title") or "Untitled") for v in videos[:3])
        more = f" (+{len(videos) - 3} more)" if len(videos) > 3 else ""
        offline_note = (
            "> **Offline index.** No transcripts were read and no summary was generated. Run without "
            "`--offline` and with `GEMINI_API_KEY` set to synthesize this volume.\n"
        )
        body = [f"# {domain}{vol_label} — Domain Compendium (Offline Index)\n", offline_note]
        link = notebook_url(notebook_id)
        if link:
            body.append(f"> **Source Notebook:** [{notebook_title}]({link})\n")
        body += [
            "## 1. Summary\n",
            f'This volume indexes {len(videos)} videos grouped under "{domain}": {sample}{more}.\n',
            "## 2. Video Index\n",
        ]
        for i, v in enumerate(videos, 1):
            title = str(v.get("title") or f"Video {i}")
            url = str(v.get("url") or "")
            heading = f"[{title}]({url})" if url else title
            body.append(
                f"### {i}. {heading}\n- **Source ID:** `{v.get('id') or v.get('source_id') or f'src_{i}'}`\n"
            )
        if related:
            body.append(f"{SECTION_RELATED}\n")
            body.extend(f"- [[{slug}|{name}]]" for slug, name in related)
            body.append("")
        return self._frontmatter(
            domain, volume, len(videos), notebook_title, notebook_id, "offline-index"
        ) + "\n".join(body)

    # -- index, catalog, append --

    def generate_index(
        self,
        compendiums: list[DomainCompendium],
        notebook_title: str = "Untitled",
        total_sources: int = 0,
    ) -> str:
        """Table of contents for every generated volume."""
        live = sum(1 for c in compendiums if c.is_live_synthesized)
        summary_line = (
            f"> {total_sources} videos grouped into {len(compendiums)} compendium volumes "
            f"({live} synthesized with Gemini, {len(compendiums) - live} offline indexes).\n"
        )
        lines = [
            "---",
            f"title: {yaml_str(f'{notebook_title} - Master Compendium Index')}",
            f"notebook_title: {yaml_str(notebook_title)}",
            f"total_compendiums: {len(compendiums)}",
            f"total_sources_distilled: {total_sources}",
            f"generated_at: {yaml_str(_now())}",
            f"generator: {yaml_str(f'Notebook Alchemy v{__version__}')}",
            "---",
            "",
            f"# {notebook_title} — Master Compendium Index\n",
            summary_line,
            "## Compendiums\n",
            "| Compendium | Volume | Videos | Est. Tokens | Mode | File |",
            "| :-- | :-: | :-: | :-: | :-- | :-- |",
        ]
        for c in compendiums:
            filename = c.relative_path.split("/")[-1]
            mode = "gemini" if c.is_live_synthesized else "offline index"
            lines.append(
                f"| **{c.domain}** | {c.volume} | {len(c.titles)} | ~{c.token_count:,} | {mode} "
                f"| [{filename}]({filename}) |"
            )
        return "\n".join(lines) + "\n"

    def generate_master_sources_catalog(
        self,
        videos: list[dict[str, Any]],
        notebook_title: str = "Untitled",
    ) -> str:
        """Ledger of every source video and its domain."""
        lines = [
            "---",
            f"title: {yaml_str(f'{notebook_title} - Master Source Catalog')}",
            f"notebook_title: {yaml_str(notebook_title)}",
            f"total_sources: {len(videos)}",
            f"generated_at: {yaml_str(_now())}",
            f"generator: {yaml_str(f'Notebook Alchemy v{__version__}')}",
            "tags:",
            "  - notebooklm",
            "  - master-source-ledger",
            "---",
            "",
            f"# {notebook_title} — Master Source Catalog (Immutable Ledger)\n",
            f"> Ledger of all {len(videos)} source videos. Appended entries are marked `+`.\n",
            "| # | Source ID | Video Title | Domain | URL |",
            "| :-: | :-: | :-- | :-- | :-- |",
        ]
        for i, v in enumerate(videos, 1):
            vid = v.get("id") or f"src_{i}"
            title = str(v.get("title") or f"Video {i}").replace("|", "-")
            url = v.get("url") or ""
            topic = str(v.get("topic") or "General").replace("|", "-")
            link = f"[Link]({url})" if url else ""
            lines.append(f"| {i} | `{vid}` | {title} | {topic} | {link} |")
        return "\n".join(lines) + "\n"

    def append_to_domain_compendium(
        self,
        existing_content: str,
        new_videos: list[dict[str, Any]],
        domain: str = "General",
    ) -> str:
        """Add newly published videos to an existing compendium and bump its source count."""
        if not new_videos:
            return existing_content
        updated = re.sub(
            r"^source_count:\s*(\d+)",
            lambda m: f"source_count: {int(m.group(1)) + len(new_videos)}",
            existing_content,
            count=1,
            flags=re.MULTILINE,
        )
        entries = []
        for i, v in enumerate(new_videos, 1):
            title = str(v.get("title") or f"New Video {i}")
            url = str(v.get("url") or "")
            heading = f"[{title}]({url})" if url else title
            entries.append(
                f"\n### [Added] {heading}\n- **Source ID:** `{v.get('id') or f'new_src_{i}'}`\n"
                f"- **Domain:** {domain}\n"
            )
        block = "\n".join(entries)
        if SECTION_RELATED in updated:
            return updated.replace(SECTION_RELATED, f"{block}\n{SECTION_RELATED}", 1)
        return updated.rstrip("\n") + "\n" + block
