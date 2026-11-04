# notebook-alchemy

[![CI](https://github.com/gagexhill/notebook-alchemy/actions/workflows/ci.yml/badge.svg)](https://github.com/gagexhill/notebook-alchemy/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A developer-first CLI tool to bypass Google NotebookLM's 300-source limit. Autonomously distill massive YouTube channels and playlists (50 to 1,500+ videos) into self-organizing, high-density Domain Compendiums.

---

## The Problem: Google NotebookLM's 300-Source Wall

Google NotebookLM enforces a strict hard ceiling of **300 sources per notebook**. 

When you attempt to ingest an entire YouTube channel backlog, course archive, or conference playlist, you hit this ceiling almost immediately. Worse, dumping hundreds of raw, individual video links causes severe practical issues:
- **Notebook Capacity Exhaustion**: A single 300-video channel consumes 100% of your source slots, leaving zero room for user notes, PDFs, or research documents.
- **RAG Retrieval Degradation**: Querying across hundreds of fragmented, conversational transcripts produces shallow, disjointed answers.
- **Context Fragmentation**: Critical principles and SOPs scattered across multiple videos never get synthesized into a cohesive playbook.

```text
Massive Video Archive (50 to 1,500+ Videos)
                    │
                    ▼
         [ 1. Channel Discovery ]  yt-dlp flat metadata extraction (<5s)
                    │
                    ▼
       [ 2. Transcript Cache ]     youtube-transcript-api (SHA-256 local cache)
                    │
                    ▼
     [ 3. Thematic Clustering ]    Dynamic domain grouping & volume balancing
                    │
                    ▼
  [ 4. Information Alchemist ]    Gemini Flash synthesis (Zero Search Grounding)
                    │
                    ▼
   [ 5. Artifact Generation ]     8–15 Domain Compendiums + Master Source Catalog
                    │
                    ▼
    [ 6. Google NotebookLM ]      Fits comfortably under 300-source limit (280+ free slots)
```

`notebook-alchemy` solves this at arbitrary scale: it scans channel transcripts, clusters related videos into coherent domain themes, and condenses hundreds to thousands of videos into a curated collection of structured Domain Compendiums—complete with actionable SOPs, mental models, video citations, and an immutable Master Source Catalog.

```text
50 to 1,500+ Raw YouTube Links  ──►  notebook-alchemy  ──►  8–15 Domain Compendiums + Master Catalog
(exceeds or fills 300-source cap)                          (280+ free slots remaining in NotebookLM)
```

---

## Features

- **Bypass the 300-Source Limit**: Scale-free ingestion partitions massive video backlogs (50 to 1,500+ videos) into balanced domain volumes (Vol. 1, Vol. 2 for 50+ video topics), staying well below NotebookLM's ceiling.
- **Immutable Master Source Catalog**: Automatically builds `MASTER_SOURCES_CATALOG.md` preserving full provenance (video titles, direct URLs, timestamps, duration, and domain mappings).
- **Incremental Auto-Append (`append`)**: When a channel releases new videos, append them to existing compendiums without re-distilling the entire channel archive.
- **Ultra-Low Cost Distillation**: Powered by Gemini Flash with zero search-grounding overhead (~$0.10 to $0.40 per 1,000 video transcripts).
- **Safe Dry-Run Preview (`dry-run`)**: Inspect video counts, domain clusters, and exact token cost estimates before touching anything in NotebookLM.
- **100% Automated Self-Verification (`test`)**: Single command `notebook-alchemy test` verifies ingestion, clustering, synthesis, and vault exports locally without credentials or API keys.
- **Zero Credential Leaks**: Browser session cookies and API keys stay local and are strictly git-ignored.
- **Obsidian & Markdown Vault Ready**: Generates a clean Markdown vault with standard frontmatter, ready for NotebookLM sync or local knowledge bases.

---

## Quickstart

### Installation

```bash
git clone https://github.com/gagexhill/notebook-alchemy.git
cd notebook-alchemy
pip install -e .
```

### 1. Verify Pipeline (100% Automated Self-Test)

Run the built-in diagnostic test suite:

```bash
notebook-alchemy test
```

This verifies configuration loading, multi-source channel cataloging, thematic clustering, compendium synthesis, and vault artifact integrity.

### 2. Optional: Set your Gemini API key (for live LLM synthesis)

Create a local `.env` file (this is strictly git-ignored):

```bash
cp .env.example .env
```

Add your Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey):

```env
GEMINI_API_KEY=your_key_here
```

*(If no key is set or if `--offline` is passed, `notebook-alchemy` uses deterministic semantic distillation).*

---

## Usage

### 1. Distill an Entire YouTube Channel or Playlist (Arbitrary Scale: 50 to 1,500+ Videos)

Pass any channel handle, playlist URL, or channel link directly to the CLI:

```bash
notebook-alchemy run https://www.youtube.com/@Portableentrepreneur
```

`notebook-alchemy` will automatically:
1. Resolve all video IDs, titles, and durations in seconds using flat metadata extraction (zero video bytes downloaded).
2. Fetch or load cached video transcripts from local storage (`~/.notebook-alchemy/cache`).
3. Group videos into 8–15 cohesive domain clusters, automatically splitting topics with 50+ videos into balanced sub-volumes.
4. Synthesize structured Domain Compendiums formatted with executive summaries, mental models, SOPs, and video citations.
5. Generate the immutable `MASTER_SOURCES_CATALOG.md` tracking all original video links.
6. Create the notebook in Google NotebookLM (if not already created) and upload all compendiums as clean sources.

### 2. Preview Video Counts, Clusters & Token Costs First (`dry-run`)

Inspect video counts, domain clusters, and estimated Gemini Flash token costs without modifying anything:

```bash
# Preview from a live YouTube channel URL
notebook-alchemy dry-run https://www.youtube.com/@Portableentrepreneur

# Or preview an existing NotebookLM notebook
notebook-alchemy dry-run -n "Example Notebook"
```

Output:

```text
╭──────────────────────── Dry-Run Cost & Token Sizing ─────────────────────────╮
│ Target Source: https://www.youtube.com/@Portableentrepreneur                 │
│ Total Videos Discovered: 363                                                 │
│                                                                              │
│ Total Estimated Tokens: ~1,633,500                                           │
│ Estimated Gemini Flash Input Cost: $0.1225 USD                               │
│ Estimated Synthesis Output Cost: $0.0545 USD                                 │
│ Total Estimated Cost: $0.1770 USD                                            │
╰──────────────────────────────────────────────────────────────────────────────╯

Domain Topic Clusters:
• Lead Generation & Client Acquisition (110 videos) -> Split into Vol. 1 & Vol. 2
• Web Design & Agency Operations (104 videos)       -> Split into Vol. 1 & Vol. 2
• SEO & Local Search (53 videos)                    -> Split into Vol. 1 & Vol. 2
• Solo Operator Systems & Productivity (28 videos)
• Pricing & Proposal Strategy (15 videos)
• Client Retention, Onboarding & QBRs (11 videos)
• General / Specialized Topics (42 videos)
```

### 3. Incrementally Append Newly Published Videos

When a channel publishes new content, append it to your existing notebook sources without re-synthesizing the entire channel archive:

```bash
notebook-alchemy append -n "Example Notebook" \
  --title "How to Sell $2,500/mo SEO Retainers" \
  --url "https://youtube.com/watch?v=sample_id" \
  --topic "Pricing & Proposal Strategy"
```

---

## CLI Reference

| Command | Description |
| :--- | :--- |
| `notebook-alchemy run <URL or name>` | Distill any YouTube channel or playlist (50 to 1,500+ videos) into NotebookLM |
| `notebook-alchemy dry-run <URL or name>` | Preview video count, domain clusters, and token cost before running |
| `notebook-alchemy clean` | Zero-bloat cleanup: purge cached transcripts and local build artifacts |
| `notebook-alchemy append` | Incrementally append newly published videos to the Master Catalog and compendiums |
| `notebook-alchemy sync -n <name>` | Sync generated vault compendiums directly into Google NotebookLM |
| `notebook-alchemy test` | Run 100% automated self-verification of the entire pipeline |
| `notebook-alchemy status` | Inspect engine configuration, vault path, and session state |
| `notebook-alchemy login --check` | Verify whether stored Google NotebookLM credentials are valid |
| `notebook-alchemy login --browser-cookies edge` | Non-interactive cookie import from Edge |

---

## Contributing

Pull requests and issues are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for local development setup and guidelines.

```bash
# Run unit & integration test suite
py -m pytest

# Run linter & formatter
py -m ruff check .
py -m ruff format .
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
