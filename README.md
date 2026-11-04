# notebook-alchemy

[![CI](https://github.com/gagexhill/notebook-alchemy/actions/workflows/ci.yml/badge.svg)](https://github.com/gagexhill/notebook-alchemy/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A developer-first CLI tool to overcome Google NotebookLM source limits and eliminate transcript fragmentation. Autonomously distill massive YouTube channels, playlists, and video archives of arbitrary scale into self-organizing, high-density Domain Compendiums.

---

## The Problem: Google NotebookLM Source Caps & Transcript Fragmentation

Google NotebookLM enforces strict per-notebook source caps that get exhausted quickly when importing raw video links. More critically, dumping hundreds or thousands of uncurated video URLs directly into a notebook causes fundamental architectural bottlenecks:
- **Notebook Source Capacity Exhaustion**: Ingesting raw individual video links rapidly consumes your notebook's available source quota, leaving zero room for user notes, research PDFs, code repositories, or reference documents.
- **RAG Retrieval Degradation & Noise**: Vector retrieval across hundreds of fragmented, conversational video transcripts yields shallow, disjointed answers saturated with spoken pleasantries and conversational filler.
- **Context & Playbook Fragmentation**: High-value mental models, strategies, and operational SOPs scattered across separate videos never get synthesized into a coherent, actionable knowledge base.
- **Sub-Optimal Audio Overviews**: NotebookLM's flagship Studio features (Audio Overview dual-host podcasts, Study Guides, Quizzes) perform best when grounded in dense, cohesive domain textbooks rather than hundreds of disconnected video snippets.

```text
Massive Video Archive (Arbitrary Scale: Dozens to Thousands of Videos)
                    │
                    ▼
         [ 1. Channel Discovery ]      yt-dlp flat metadata extraction (<15s, 0 media bytes)
                    │
                    ▼
        [ 2. Transcript Resolver ]     Multi-source: local .txt/.vtt/.srt + cache + YouTube captions
                    │
                    ▼
      [ 3. Emergent Clustering ]       Zero-shot Gemini Flash domain induction + deterministic regex
                    │
                    ▼
   [ 4. Information Alchemist ]        Full transcript synthesis into Gemini Flash 1M context window
                    │
                    ▼
    [ 5. Artifact Generation ]         8–18 Domain Compendiums + Master Source Catalog (INDEX.md)
                    │
                    ▼
     [ 6. Google NotebookLM ]          Compact source footprint (85%–98% notebook capacity preserved)
```

`notebook-alchemy` solves this at arbitrary scale: it scans channel transcripts, clusters related videos into cohesive domain themes, and condenses massive video archives into a curated collection of structured Domain Compendiums—complete with actionable SOPs, mental models, video citations, and an immutable Master Source Catalog.

```text
Dozens to 1,000s+ Raw Video Links  ──►  notebook-alchemy  ──►  8–18 Domain Compendiums + Master Catalog
(exhausts notebook source quotas)                              (85%–98% source capacity preserved for notes & docs)
```

---

## Core Features & Architectural Pillars

### 1. Universal Zero-Shot Emergent Domain Discovery
Unlike rigid category tools, `notebook-alchemy` dynamically adapts to **any subject matter or genre**. Whether your target channel focuses on:
- Software engineering, DevOps, or web architecture
- Early childhood development and parenting
- Relationship dynamics and psychology
- Astrophysics, biology, or medicine
- Small business agency growth and lead generation

The engine leverages Gemini Flash for zero-shot domain induction, clustering titles and concepts into 6–15 natural thematic pillars with dynamic volume splitting (`Vol. 1`, `Vol. 2` for topics exceeding 40–50 videos). Offline deterministic regex classification is built-in as an instant fallback.

### 2. Multi-Source Transcript Resolver
Never lose insights to missing captions. The transcript engine resolves transcripts through a resilient waterfall:
1. **Local Transcript Files**: Scans local directories for `.txt`, `.vtt`, or `.srt` files matching video IDs or sanitized titles.
2. **Persistent SHA-256 Cache**: Checks `~/.notebook-alchemy/cache` to eliminate redundant network requests.
3. **YouTube Captions API**: Ingests official or auto-generated caption tracks via `youtube-transcript-api`.
4. **Lightweight Audio Stream Fallback**: Available for channels where caption tracks are unavailable.

### 3. Direct Full-Transcript Synthesis (1M Token Context)
Compendiums are synthesized by streaming full verbatim spoken dialogue (~120,000+ tokens per domain volume) directly into Gemini Flash's 1-million-token context window. Instead of superficial one-paragraph summaries, the alchemist extracts:
- **Verbatim Frameworks & Mental Models**: The creator's exact mental models and strategic taxonomies.
- **Step-by-Step SOPs**: Actionable, executable operational procedures with code snippets, prompts, or checklists.
- **Common Anti-Patterns & Traps**: Explicit "what NOT to do" callouts.
- **Immutable Provenance Citations**: Every principle links back directly to the source video and timestamp.

### 4. NotebookLM Studio Ready
Engineered specifically to maximize Google NotebookLM's flagship Studio capabilities:
- **Audio Overviews (Deep Dive Podcast)**: High-density compendiums give the dual AI hosts rich, structured narratives to discuss rather than repeating disjointed intro banter.
- **Study Guides & Briefing Docs**: Cohesive domain volumes provide clean section hierarchies for structured study guides.
- **Quizzes & FAQs**: Compendiums preserve nuanced operational details needed for rigorous self-assessment.

### 5. Upfront Safety Gates & Interactive Confirmation
- **Upfront Authentication Check**: The CLI checks NotebookLM session credentials *before* running compute or LLM synthesis, preventing wasted API quota. Use `--no-sync` or `--preview-only` to run purely local builds anytime.
- **Interactive Notebook Naming**: Displays the detected channel name and lets you accept or customize the destination Google NotebookLM notebook name right from the terminal.

### 6. Zero-Bloat Ephemeral Cleanup (`clean`)
Keep your local machine lean. Reclaim disk space by purging cached transcripts or local markdown builds with a single flag:
```bash
notebook-alchemy clean --all       # Purges transcript cache and local vaults
notebook-alchemy clean --cache     # Purges ~/.notebook-alchemy/cache
notebook-alchemy clean --vaults    # Purges ~/.notebook-alchemy/vaults
```

---

## Empirical Benchmark & Scale Verification

The pipeline has been empirically benchmarked across three channel tiers to guarantee performance, slot conservation, and cost efficiency:

| Metric | Tier 1: Small (`@Hyperplexed`) | Tier 2: Medium (`@Portableentrepreneur`) | Tier 3: Large (`@TraversyMedia`) |
| :--- | :--- | :--- | :--- |
| **Discovered Video Count** | **52 videos** | **363 videos** | **1,037 videos** |
| **Channel Genre** | Frontend UI / Creative CSS | Agency Growth / SEO / Web | Full-Stack Web Dev / DevOps |
| **Flat Discovery Speed** | **1.21s** | **5.39s** | **14.61s** |
| **Total Estimated Tokens** | ~234,000 tokens | ~1,633,500 tokens | ~4,666,500 tokens |
| **Gemini Flash Distillation Cost** | **$0.0253 USD** | **$0.1769 USD** | **$0.5055 USD** |
| **Active Domain Clusters** | 6 clusters | 13 clusters | 14 clusters |
| **Synthesized Compendium Volumes** | 6 volumes | 18 volumes | 33 volumes |
| **NotebookLM Source Footprint** | 7 sources (6 vols + Catalog) | 19 sources (18 vols + Catalog) | 34 sources (33 vols + Catalog) |
| **NotebookLM Source Reduction** | **86.5% reduction** (52 vids -> 7 sources) | **94.8% reduction** (363 vids -> 19 sources) | **96.7% reduction** (1,037 vids -> 34 sources) |
| **Notebook Capacity Preserved** | **>85% capacity free** for notes & PDFs | **>94% capacity free** for notes & PDFs | **>96% capacity free** for notes & PDFs |
| **Unclassified Leakage Rate** | 21.2% (11 videos) | 16.5% (60 videos) | 15.9% (165 videos) |

*Full evaluation criteria and methodology are documented in [`test_benchmark_matrix.md`](test_benchmark_matrix.md).*

---

## Quickstart

### Installation

```bash
git clone https://github.com/gagexhill/notebook-alchemy.git
cd notebook-alchemy
pip install -e .
```

### 1. Verify Pipeline (100% Automated Self-Test)

Run the built-in diagnostic test suite without needing credentials or API keys:

```bash
notebook-alchemy test
```

This verifies configuration loading, multi-source channel cataloging, thematic clustering, compendium synthesis, and vault artifact integrity.

### 2. Optional: Set your Gemini API Key

Create a local `.env` file (strictly git-ignored):

```bash
cp .env.example .env
```

Add your Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey):

```env
GEMINI_API_KEY=your_key_here
```

*(If no key is set or `--offline` is passed, `notebook-alchemy` runs deterministic semantic distillation).*

### 3. Authenticate with Google NotebookLM

Import session cookies from your default browser or launch a secure browser instance:

```bash
# Non-interactive cookie import from Edge or Chrome
notebook-alchemy login --browser-cookies edge

# Or launch interactive Chromium login
notebook-alchemy login --browser chromium

# Verify session authentication
notebook-alchemy login --check
```

---

## Usage

### 1. Distill an Entire YouTube Channel or Playlist (Arbitrary Scale)

Pass any channel handle, playlist URL, or channel link directly to the CLI:

```bash
notebook-alchemy run https://www.youtube.com/@Portableentrepreneur
```

Interactive confirmation prompt:
```text
[?] Target NotebookLM notebook name [Example Notebook]:
```
Press **Enter** to accept the default or type a custom notebook name.

`notebook-alchemy` will:
1. Validate your NotebookLM login upfront.
2. Resolve all video metadata in seconds (flat extraction, zero video downloads).
3. Ingest transcripts from local files, cache, or captions API.
4. Cluster videos into coherent domain pillars, splitting 40+ video clusters into balanced volumes.
5. Synthesize rich Markdown compendiums with mental models, SOPs, and source attribution.
6. Generate the immutable `MASTER_SOURCES_CATALOG.md` and `INDEX.md`.
7. Create or open the notebook in NotebookLM and upload all compendiums.

### 2. Preview Video Counts, Clusters & Token Costs (`dry-run`)

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

### 3. Run Local-Only Distillation (Without NotebookLM Sync)

Build the local Markdown compendiums in `~/.notebook-alchemy/vaults/` without uploading to Google NotebookLM:

```bash
notebook-alchemy run https://www.youtube.com/@Hyperplexed --no-sync
```

### 4. Incrementally Append Newly Published Videos

When a creator releases a new video, append it to your existing notebook sources without re-synthesizing the entire channel archive:

```bash
notebook-alchemy append -n "Example Notebook" \
  --title "How to Sell $2,500/mo SEO Retainers" \
  --url "https://youtube.com/watch?v=sample_id" \
  --topic "Pricing & Proposal Strategy"
```

### 5. Clean Up Caches & Vaults

Reclaim disk space anytime:

```bash
# Purge transcript cache and local vault markdown files
notebook-alchemy clean --all

# Or purge only the transcript cache
notebook-alchemy clean --cache
```

---

## CLI Reference

| Command | Key Options | Description |
| :--- | :--- | :--- |
| `notebook-alchemy run <target>` | `-N, --notebook-name`<br>`--sync / --no-sync`<br>`--offline`<br>`--preview-only` | Distill any YouTube channel, playlist, or video archive of arbitrary scale into NotebookLM |
| `notebook-alchemy dry-run <target>` | `-n, --name` | Preview video counts, domain clusters, and token costs before executing |
| `notebook-alchemy clean` | `--all`<br>`--cache`<br>`--vaults` | Zero-bloat disk cleanup: purge cached transcripts and local build artifacts |
| `notebook-alchemy append` | `-n, --name`<br>`--title`<br>`--url`<br>`--topic` | Incrementally append newly published videos to Master Catalog and compendiums |
| `notebook-alchemy sync -n <name>` | `--vault-path` | Sync generated local vault compendiums directly into Google NotebookLM |
| `notebook-alchemy test` | | Run 100% automated self-verification of the entire pipeline locally |
| `notebook-alchemy status` | | Inspect engine configuration, vault path, cache size, and session state |
| `notebook-alchemy login` | `--check`<br>`--browser-cookies`<br>`--browser` | Safe session authentication for Google NotebookLM without credential leaks |

---

## Contributing

Pull requests and issues are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for local development setup and guidelines.

```bash
# Run unit & integration test suite (24 tests)
py -m pytest

# Run linter & code style checks
py -m ruff check .
py -m ruff format --check .
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
