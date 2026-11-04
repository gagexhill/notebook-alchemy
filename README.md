# notebook-alchemy

[![CI](https://github.com/gagexhill/notebook-alchemy/actions/workflows/ci.yml/badge.svg)](https://github.com/gagexhill/notebook-alchemy/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A developer-first CLI tool to bypass Google NotebookLM's 300-source limit.

If you add an entire YouTube playlist or channel backlog to NotebookLM, you will hit the hard 300-source cap immediately. NotebookLM slows down, chat context gets fragmented, and you run out of slots for any new notes or documents.

`notebook-alchemy` solves this: it scans your video transcripts, clusters related videos into coherent domain themes, and condenses hundreds of scattered video links into clean, high-density Domain Compendiums with actionable SOPs, mental models, and video citations.

```text
288 raw YouTube links  ──►  notebook-alchemy  ──►  10 domain compendiums
(fills notebook to cap)                              (290 free slots remaining)
```

---

## Features

- **Bypass the 300-source wall**: Turn hundreds of video links into 6–10 structured reference compendiums.
- **Safe dry-run preview**: See video counts, domain clusters, and token cost estimates before touching anything in NotebookLM.
- **Cheap & fast**: Uses Gemini Flash for synthesis (~$0.14 total for 288 video transcripts, ~1.3M tokens).
- **Automated pipeline verification**: Single command `notebook-alchemy test` verifies the entire ingestion, clustering, and vault synthesis flow without manual steps.
- **Zero credential leaks**: Browser session cookies and API keys stay local and are strictly git-ignored.
- **Markdown vault export**: Exports formatted `.md` compendiums and a master `INDEX.md` ready for NotebookLM re-upload or Obsidian vaults.

---

## Quickstart

### Installation

```bash
git clone https://github.com/gagexhill/notebook-alchemy.git
cd notebook-alchemy
pip install -e .
```

### 1. Verify Pipeline (100% Automated Self-Test)

Run the built-in end-to-end verification test:

```bash
notebook-alchemy test
```

This verifies configuration loading, the 288-source catalog, thematic clustering, compendium synthesis, and vault artifact integrity.

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

### 1. Distill an Entire YouTube Channel (288, 1,000, or 1,670+ Videos)

Pass any channel handle, playlist, or channel URL directly to the CLI:

```bash
notebook-alchemy run https://www.youtube.com/@Portableentrepreneur
```

`notebook-alchemy` will automatically:
1. Scan the channel and resolve all video IDs, titles, and durations without downloading video bytes.
2. Fetch or load cached video transcripts.
3. Group videos into 6–12 authoritative domain clusters.
4. Synthesize structured Domain Compendiums (batched into volumes like Vol. 1, Vol. 2 for 50+ video topics).
5. Generate the immutable `MASTER_SOURCES_CATALOG.md` tracking all original video links.
6. Automatically create the notebook in Google NotebookLM and upload all compendiums as sources.

### 2. Preview a Channel or Notebook First (`dry-run`)

Inspect video counts, domain clusters, and estimated Gemini Flash token costs without modifying anything:

```bash
# Preview from a live YouTube channel URL
notebook-alchemy dry-run https://www.youtube.com/@Portableentrepreneur

# Or preview an existing NotebookLM notebook
notebook-alchemy dry-run -n "Portable Entrepreneur"
```

Output:

```text
╭──────────────────────── Dry-Run Cost & Token Sizing ─────────────────────────╮
│ Notebook: Portable Entrepreneur (nb_portableentrepreneur) (Live NotebookLM)  │
│ Total Sources: 363                                                           │
│ YouTube Video Sources: 363                                                   │
│                                                                              │
│ Total Estimated Tokens: ~1,633,500                                           │
│ Estimated Gemini Flash Input Cost: $0.1225 USD                               │
│ Estimated Synthesis Output Cost: $0.0545 USD                                 │
│ Total Estimated Cost: $0.1770 USD                                            │
╰──────────────────────────────────────────────────────────────────────────────╯

Domain Topic Clusters:
• Lead Generation & Client Acquisition (110 videos)
• Web Design & Agency Operations (104 videos)
• SEO & Local Search (53 videos)
• Solo Operator Systems & Productivity (28 videos)
• Pricing & Proposal Strategy (15 videos)
• Client Retention, Onboarding & QBRs (11 videos)
• General / Specialized Topics (42 videos)
```

### 3. Incrementally Append New Videos as They Are Published

When the channel releases a new video, append it to your existing notebook sources without re-synthesizing everything from scratch:

```bash
notebook-alchemy append -n "Portable Entrepreneur" \
  --title "How to Sell $2,500/mo SEO Retainers" \
  --url "https://youtube.com/watch?v=sample_id" \
  --topic "Pricing & Proposal Strategy"
```

---

## CLI Reference

| Command | Description |
| :--- | :--- |
| `notebook-alchemy run <URL or name>` | Distill 288 to 1,670+ videos from a YouTube channel into NotebookLM |
| `notebook-alchemy dry-run <URL or name>` | Preview video count, domain clusters, and token cost before running |
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
python -m unittest discover tests

# Run linter & formatter
ruff check .
ruff format .
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
