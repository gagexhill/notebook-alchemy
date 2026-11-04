"""Domain Compendium synthesis engine for Notebook Alchemy."""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import __version__
from .channel import TranscriptManager

logger = logging.getLogger(__name__)

GEMINI_API_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
TRANSCRIPT_CHARACTER_BUDGET = 160_000


@dataclass
class DomainCompendium:
    """Represents a fully synthesized domain reference compendium."""

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
    """Synthesizes clusters of video transcripts into structured Domain Compendiums."""

    def __init__(self, api_key: str | None = None, model: str = "gemini-2.5-flash") -> None:
        self.api_key = api_key or ""
        self.model = model

    def _is_valid_api_key(self) -> bool:
        """Check whether the provided API key is genuine or a placeholder."""
        key = self.api_key.strip()
        if not key or key == "your_gemini_api_key_here" or key.startswith("your_"):
            return False
        return len(key) >= 15

    def _safe_filename(self, text: str) -> str:
        """Convert domain text to a clean filesystem-safe name."""
        cleaned = re.sub(r"[^\w\s-]", "", text).strip()
        return re.sub(r"[-\s]+", "_", cleaned)

    def synthesize_cluster(
        self,
        domain: str,
        videos: list[dict[str, Any]],
        volume: int = 1,
        *,
        force_offline: bool = False,
        notebook_title: str = "Portable Entrepreneur",
        notebook_id: str = "",
    ) -> DomainCompendium:
        """Synthesize a cluster of video sources into a complete Domain Compendium."""
        titles = [str(v.get("title") or "Untitled") for v in videos]
        source_ids = [
            str(v.get("id") or v.get("source_id") or f"src_{i}") for i, v in enumerate(videos)
        ]

        safe_name = self._safe_filename(domain)
        vol_suffix = f"_Vol_{volume}" if volume > 1 else ""
        rel_path = f"compendiums/{safe_name}{vol_suffix}.md"

        content = ""
        is_live = False
        warnings: list[str] = []

        if not force_offline and self._is_valid_api_key():
            try:
                content, warnings = self._call_gemini_api(domain, videos, volume, notebook_title)
                is_live = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Live Gemini API synthesis encountered error (%s); falling back to offline semantic synthesis.",
                    exc,
                )
                content = self._generate_offline_compendium(
                    domain, videos, volume, notebook_title, notebook_id
                )
                warnings.append("Live synthesis failed; generated a title-only index.")
        else:
            content = self._generate_offline_compendium(
                domain, videos, volume, notebook_title, notebook_id
            )

        # Rough token approximation (4 characters per token)
        token_count = max(len(content) // 4, 1)

        return DomainCompendium(
            domain=domain,
            volume=volume,
            titles=titles,
            source_ids=source_ids,
            relative_path=rel_path,
            content=content,
            token_count=token_count,
            is_live_synthesized=is_live,
            warnings=warnings,
        )

    def _call_gemini_api(
        self,
        domain: str,
        videos: list[dict[str, Any]],
        volume: int,
        notebook_title: str,
    ) -> tuple[str, list[str]]:
        """Invoke the Google Gemini API to generate the synthesis with full spoken transcript text."""
        url = GEMINI_API_ENDPOINT.format(model=self.model) + f"?key={self.api_key}"

        transcript_mgr = TranscriptManager()
        sources_payload = []
        warnings: list[str] = []
        per_video_budget = TRANSCRIPT_CHARACTER_BUDGET // max(len(videos), 1)
        for v in videos:
            title = v.get("title") or "Untitled"
            url_str = v.get("url") or "(URL unavailable)"
            vid_id = v.get("id") or v.get("source_id") or ""
            raw_text = v.get("transcript") or transcript_mgr.fetch_transcript_text(vid_id, title)
            if raw_text and len(raw_text) > per_video_budget:
                warnings.append(f"Transcript truncated to {per_video_budget} characters: {title}")
            snippet = (
                raw_text[:per_video_budget].strip()
                if raw_text
                else "(Transcript unavailable; do not infer content from the title)"
            )
            sources_payload.append(
                f"### Video: [{title}]({url_str})\nVideo ID: {vid_id}\n\nSpoken Transcript Content:\n{snippet}\n"
            )

        sources_text = "\n---\n".join(sources_payload)

        prompt = (
            f"You are a Principal Knowledge Architect and Information Alchemist.\n"
            f"Synthesize the following {len(videos)} video sources from '{notebook_title}' into an authoritative, "
            f"high-density Domain Compendium for the domain '{domain}' (Volume {volume}) for Google NotebookLM.\n\n"
            f"Requirements:\n"
            f"1. Produce a high-density Markdown Domain Compendium designed specifically for Google NotebookLM ingestion.\n"
            f"2. Extract deep operational SOPs, mental models, frameworks, and verbal scripts directly from the spoken transcripts.\n"
            f"3. Eliminate conversational filler, sponsor plugs, and intro music.\n"
            f"4. Provide a structured Table of Contents, Executive Summary, Detailed Protocols, Common Pitfalls, and a complete Source Attribution Table.\n\n"
            f"Transcript budget: {TRANSCRIPT_CHARACTER_BUDGET} characters total, "
            f"up to {per_video_budget} characters per video.\n\n"
            f"Source Videos & Spoken Transcripts:\n\n{sources_text}"
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
            },
        }

        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError("Empty response from Gemini API")
            parts = candidates[0].get("content", {}).get("parts", [])
            if candidates[0].get("finishReason") == "MAX_TOKENS":
                warnings.append("Gemini output was truncated (MAX_TOKENS).")
            text = "".join(str(part.get("text", "")) for part in parts)
            if not text.strip():
                raise RuntimeError("Empty response from Gemini API")
            return text + "\n\n" + self._source_table(videos), warnings

    @staticmethod
    def _source_table(videos: list[dict[str, Any]]) -> str:
        lines = ["## Source Index", "", "| Title | Source ID | URL |", "| --- | --- | --- |"]
        for i, video in enumerate(videos):
            title = str(video.get("title") or "Untitled").replace("|", "\\|").replace("\n", " ")
            source_id = str(video.get("id") or video.get("source_id") or f"src_{i}").replace(
                "|", "\\|"
            )
            url = str(video.get("url") or "").replace("|", "%7C")
            lines.append(f"| {title} | {source_id} | {url} |")
        return "\n".join(lines)

    def _generate_offline_compendium(
        self,
        domain: str,
        videos: list[dict[str, Any]],
        volume: int,
        notebook_title: str,
        notebook_id: str,
    ) -> str:
        """Generate an index from available source metadata without inventing content."""
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        vol_label = f" (Vol. {volume})" if volume > 1 else ""
        header_lines = [
            "---",
            f'title: "{domain}{vol_label} - Source Index"',
            f'domain: "{domain}"',
            f"volume: {volume}",
            f'notebook_title: "{notebook_title}"',
            f"source_count: {len(videos)}",
            f'generated_at: "{now}"',
            f'generator: "Notebook Alchemy v{__version__}"',
            "---\n",
        ]
        return "\n".join(header_lines) + (
            f"# {domain}{vol_label} — Source Index\n\n"
            "> Offline title-only index. Transcripts were not synthesized.\n\n"
            + self._source_table(videos)
            + "\n"
        )

    def _get_domain_executive_summary(self, domain: str) -> str:
        summaries = {
            "Lead Generation & Client Acquisition": (
                "Systematic client acquisition for boutique service agencies and solo operators. "
                "Replaces sporadic cold outreach with compounding inbound authority, hyper-targeted "
                "local audits, and high-converting video pitch methodologies. Prioritizes pre-qualified "
                "prospecting over volume spray-and-pray."
            ),
            "Web Design & Agency Operations": (
                "End-to-end operational blueprint for modern web design practices. Covers technical "
                "stack selection (WordPress, Webflow), wireframing, client deliverable scoping, and "
                "eliminating scope creep through structured milestone sign-offs."
            ),
            "Client Retention, Onboarding & QBRs": (
                "Systematized client lifecycle management designed to maximize Lifetime Value (LTV). "
                "Focuses on structured 90-day Quarterly Business Reviews (QBRs), transparent client "
                "reporting portals, and turning one-off build projects into recurring retainers."
            ),
            "Pricing & Proposal Strategy": (
                "Value-based pricing architecture shifting away from billable hourly models toward "
                "high-tier package tiers ($3,000–$10,000+). Includes proposal psychology, anchor pricing, "
                "and closing friction reduction."
            ),
            "SEO & Local Search": (
                "Local search domination playbook centered on Google Business Profile (GBP) optimization, "
                "Local 3-Pack rank acceleration, geo-grid tracking, citations, schema markup, and "
                "packaging $1,500/mo local SEO recurring retainers."
            ),
            "Solo Operator Systems & Productivity": (
                "Architectural frameworks for running a 6-figure agency as an un-leveraged solo founder. "
                "Combines calendar time-blocking, automated invoicing/contract workflows, and proactive "
                "burnout mitigation."
            ),
        }
        return summaries.get(
            domain,
            f"Comprehensive distillation of strategic workflows, operational playbooks, and tactical frameworks for {domain}.",
        )

    def _get_domain_principles(self, domain: str) -> str:
        return (
            "- **The Rule of Specificity:** Generic offers trigger price comparison; domain-specific solutions command premium fees.\n"
            "- **Asset-Driven Delivery:** Build once, deploy repeatedly using documented SOPs and battle-tested component libraries.\n"
            "- **Asynchronous Client Guardrails:** Protect focus by establishing clear communication protocols and eliminating unscheduled calls.\n"
            "- **Outcome-Anchored Positioning:** Frame all deliverables in terms of revenue growth, lead capture, and customer retention."
        )

    def _get_domain_sops(self, domain: str) -> str:
        return (
            "### SOP-01: Pre-Engagement Discovery & Scoping\n"
            "1. Deliver standard 5-question asynchronous intake audit.\n"
            "2. Identify client revenue bottlenecks and primary conversion goals.\n"
            "3. Issue 3-tier proposal with clear boundary exclusions.\n\n"
            "### SOP-02: Deliverable Execution & Milestone Sign-Off\n"
            "1. Deploy staging environment and deliver wireframe milestone.\n"
            "2. Secure asynchronous client sign-off via annotated Loom/review portal.\n"
            "3. Launch live deployment and initiate post-launch care retainer onboarding."
        )

    def generate_index(
        self,
        compendiums: list[DomainCompendium],
        notebook_title: str = "Portable Entrepreneur",
        total_sources: int = 100,
    ) -> str:
        """Generate a master Table of Contents / Index markdown file for all compendiums."""
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        summary_quote = (
            f"> **{total_sources}** video sources grouped into **{len(compendiums)}** "
            "domain documents. Offline documents are title-only source indexes.\n\n"
        )

        lines = [
            "---",
            f'title: "{notebook_title} - Master Compendium Index"',
            f'notebook_title: "{notebook_title}"',
            f"total_compendiums: {len(compendiums)}",
            f"total_sources: {total_sources}",
            f'generated_at: "{now}"',
            f'generator: "Notebook Alchemy v{__version__}"',
            "---\n",
            f"# {notebook_title} — Master Compendium Index\n",
            summary_quote,
            "## Domain Documents\n",
            "| Compendium | Volume | Source Count | Est. Tokens | File Link |",
            "| :--- | :---: | :---: | :---: | :--- |",
        ]

        for c in compendiums:
            filename = c.relative_path.split("/")[-1]
            lines.append(
                f"| **{c.domain}** | Vol. {c.volume} | {len(c.titles)} videos | ~{c.token_count:,} | [{filename}]({filename}) |"
            )

        lines.extend(
            [
                "\n## How to Use in Google NotebookLM\n",
                "1. Upload these `.md` compendium files into your NotebookLM notebook.",
                "2. Offline indexes contain source metadata, not synthesized transcripts.",
            ]
        )

        return "\n".join(lines)

    def generate_master_sources_catalog(
        self,
        videos: list[dict[str, Any]],
        notebook_title: str = "Portable Entrepreneur",
    ) -> str:
        """Generate a single immutable master ledger document of all raw sources."""
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        ledger_notice = (
            "> **NOTICE:** This source ledger tracks all original "
            f"**{len(videos)}** video sources. Do not edit manually; all updates are auto-appended.\n\n"
        )

        lines = [
            "---",
            f'title: "{notebook_title} - Master Source Catalog"',
            f'notebook_title: "{notebook_title}"',
            f"total_sources: {len(videos)}",
            f'generated_at: "{now}"',
            "immutable: true",
            f'generator: "Notebook Alchemy v{__version__}"',
            "tags:",
            "  - notebooklm",
            "  - master-source-ledger",
            "---\n",
            f"# {notebook_title} — Master Source Catalog (Immutable Ledger)\n",
            ledger_notice,
            "| # | Source ID | Video Title | Domain | URL |",
            "| :---: | :---: | :--- | :--- | :--- |",
        ]

        for i, v in enumerate(videos, 1):
            vid_id = v.get("id") or f"src_{i}"
            title = (v.get("title") or f"Video {i}").replace("|", "-")
            url = v.get("url") or ""
            topic = v.get("topic") or "General"
            link = f"[Link]({url})" if url else ""
            lines.append(f"| {i} | `{vid_id}` | {title} | {topic} | {link} |")

        return "\n".join(lines)

    def append_to_domain_compendium(
        self,
        existing_content: str,
        new_videos: list[dict[str, Any]],
        domain: str = "General",
    ) -> str:
        """Incrementally append newly distilled video entries into an existing domain compendium."""
        if not new_videos:
            return existing_content

        # Update source count in YAML frontmatter if present
        updated_content = re.sub(
            r"source_count:\s*(\d+)",
            lambda m: f"source_count: {int(m.group(1)) + len(new_videos)}",
            existing_content,
        )

        new_entries: list[str] = []
        for i, v in enumerate(new_videos, 1):
            title = v.get("title", f"New Video {i}")
            url = v.get("url", "https://youtube.com")
            vid_id = v.get("id", f"new_src_{i}")
            new_entries.append(
                f"\n### [Auto-Appended] [{title}]({url})\n"
                f"- **Source ID:** `{vid_id}`\n"
                f"- **Core Theme:** {domain}\n"
            )

        append_block = "\n".join(new_entries)

        # Insert before section 5 if present, otherwise append at end
        if "## 5. Cross-Domain Interlinks" in updated_content:
            return updated_content.replace(
                "## 5. Cross-Domain Interlinks",
                f"{append_block}\n## 5. Cross-Domain Interlinks",
            )
        return updated_content + "\n" + append_block
