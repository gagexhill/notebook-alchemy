"""Domain Compendium synthesis engine for Notebook Alchemy."""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GEMINI_API_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


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
        notebook_title: str = "Example Notebook",
        notebook_id: str = "00000000-0000-4000-8000-0000000000aa",
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

        if not force_offline and self._is_valid_api_key():
            try:
                content = self._call_gemini_api(domain, videos, volume, notebook_title)
                is_live = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Live Gemini API synthesis encountered error (%s); falling back to offline semantic synthesis.",
                    exc,
                )
                content = self._generate_offline_compendium(
                    domain, videos, volume, notebook_title, notebook_id
                )
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
        )

    def _call_gemini_api(
        self,
        domain: str,
        videos: list[dict[str, Any]],
        volume: int,
        notebook_title: str,
    ) -> str:
        """Invoke the Google Gemini API to generate the synthesis."""
        url = GEMINI_API_ENDPOINT.format(model=self.model) + f"?key={self.api_key}"

        video_list_str = "\n".join(
            f"- [{v.get('title')}]({v.get('url', 'https://youtube.com')})" for v in videos[:50]
        )

        prompt = (
            f"You are a Principal Knowledge Architect. Synthesize the following {len(videos)} video sources "
            f"from the '{notebook_title}' collection for the domain '{domain}' (Volume {volume}).\n\n"
            f"Requirements:\n"
            f"1. Produce a high-density, authoritative Markdown Domain Compendium.\n"
            f"2. Include Executive Summary, Foundational Principles, Actionable SOPs, and Video-by-Video Breakdown.\n"
            f"3. Ensure the document is formatted for Google NotebookLM ingestion.\n\n"
            f"Sources:\n{video_list_str}"
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
            },
        }

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError("Empty response from Gemini API")
            parts = candidates[0].get("content", {}).get("parts", [])
            return str(parts[0].get("text", ""))

    def _generate_offline_compendium(
        self,
        domain: str,
        videos: list[dict[str, Any]],
        volume: int,
        notebook_title: str,
        notebook_id: str,
    ) -> str:
        """Generate a structured, rich Markdown reference compendium deterministically."""
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        vol_label = f" (Vol. {volume})" if volume > 1 else ""

        # Build clean frontmatter
        header_lines = [
            "---",
            f'title: "{domain}{vol_label} - Domain Compendium"',
            f'domain: "{domain}"',
            f"volume: {volume}",
            f'notebook_title: "{notebook_title}"',
            f'notebook_id: "{notebook_id}"',
            f"source_count: {len(videos)}",
            f'generated_at: "{now}"',
            'generator: "Notebook Alchemy v0.1.0"',
            "tags:",
            "  - notebooklm",
            "  - domain-compendium",
            f"  - {self._safe_filename(domain).lower().replace('_', '-')}",
            "---\n",
        ]

        # Domain-specific strategy synthesis
        executive_summary = self._get_domain_executive_summary(domain)
        principles = self._get_domain_principles(domain)
        sops = self._get_domain_sops(domain)

        # Build video index
        video_entries: list[str] = []
        for i, v in enumerate(videos, 1):
            title = v.get("title", f"Video {i}")
            url = v.get("url", "https://youtube.com")
            vid_id = v.get("id", f"src_{i}")
            video_entries.append(
                f"### {i}. [{title}]({url})\n"
                f"- **Source ID:** `{vid_id}`\n"
                f"- **Core Theme:** {domain}\n"
                f"- **High-Yield Takeaway:** Actionable methodology and proven workflows detailed in this session.\n"
            )

        source_notebook_line = (
            f"> **Source Notebook:** [{notebook_title}]"
            f"(https://notebooklm.google.com/notebook/{notebook_id}) "
            f"| **Distilled Sources:** {len(videos)} videos\n"
        )
        interlinks_section = (
            "- [[Lead_Generation_Client_Acquisition|Lead Generation & Client Acquisition]]\n"
            "- [[Web_Design_Agency_Operations|Web Design & Agency Operations]]\n"
            "- [[SEO_Local_Search|SEO & Local Search]]\n"
            "- [[Pricing_Proposal_Strategy|Pricing & Proposal Strategy]]\n"
            "- [[Client_Retention_Onboarding_QBRs|Client Retention, Onboarding & QBRs]]\n"
            "- [[Solo_Operator_Systems_Productivity|Solo Operator Systems & Productivity]]\n"
        )

        body = [
            f"# {domain}{vol_label} — Domain Compendium\n",
            source_notebook_line,
            "## 1. Executive Summary & Strategic Core\n",
            executive_summary,
            "\n## 2. Foundational Mental Models & Principles\n",
            principles,
            "\n## 3. Actionable Standard Operating Procedures (SOPs)\n",
            sops,
            "\n## 4. Master Cross-Source Catalog & Video Breakdown\n",
            "\n".join(video_entries),
            "\n## 5. Cross-Domain Interlinks\n",
            interlinks_section,
        ]

        return "\n".join(header_lines) + "\n".join(body)

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
        notebook_title: str = "Example Notebook",
        total_sources: int = 100,
    ) -> str:
        """Generate a master Table of Contents / Index markdown file for all compendiums."""
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        summary_quote = (
            f"> Distilled **{total_sources}** YouTube video sources into **{len(compendiums)}** "
            "authoritative, human-readable Domain Compendiums ready for Google NotebookLM.\n\n"
        )

        lines = [
            "---",
            f'title: "{notebook_title} - Master Compendium Index"',
            f'notebook_title: "{notebook_title}"',
            f"total_compendiums: {len(compendiums)}",
            f"total_sources_distilled: {total_sources}",
            f'generated_at: "{now}"',
            'generator: "Notebook Alchemy v0.1.0"',
            "---\n",
            f"# {notebook_title} — Master Compendium Index\n",
            summary_quote,
            "## Distilled Domain Compendiums\n",
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
                "2. NotebookLM will ingest each compendium as a clean, high-density source.",
                "3. Scales effortlessly to archives of 50 to 1,500+ videos while keeping your notebook well below NotebookLM's source limit.",
            ]
        )

        return "\n".join(lines)

    def generate_master_sources_catalog(
        self,
        videos: list[dict[str, Any]],
        notebook_title: str = "Example Notebook",
    ) -> str:
        """Generate a single immutable master ledger document of all raw sources."""
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        ledger_notice = (
            "> **NOTICE:** This document is an AI-generated, immutable source ledger tracking all original "
            f"**{len(videos)}** video sources. Do not edit manually; all updates are auto-appended.\n\n"
        )

        lines = [
            "---",
            f'title: "{notebook_title} - Master Source Catalog"',
            f'notebook_title: "{notebook_title}"',
            f"total_sources: {len(videos)}",
            f'generated_at: "{now}"',
            "immutable: true",
            'generator: "Notebook Alchemy v0.1.0"',
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
            url = v.get("url") or "https://youtube.com"
            topic = v.get("topic") or "General"
            lines.append(f"| {i} | `{vid_id}` | {title} | {topic} | [Link]({url}) |")

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
                f"- **High-Yield Takeaway:** Incremental workflow and newly synthesized methodologies.\n"
            )

        append_block = "\n".join(new_entries)

        # Insert before section 5 if present, otherwise append at end
        if "## 5. Cross-Domain Interlinks" in updated_content:
            return updated_content.replace(
                "## 5. Cross-Domain Interlinks",
                f"{append_block}\n## 5. Cross-Domain Interlinks",
            )
        return updated_content + "\n" + append_block
