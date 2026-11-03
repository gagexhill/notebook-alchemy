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

### 1. Preview a notebook without making changes (`dry-run`)

Run a dry run on any notebook by name or ID:

```bash
notebook-alchemy dry-run -n "Portable Entrepreneur"
```

Output:

```text
╭──────────────────────── Dry-Run Cost & Token Sizing ─────────────────────────╮
│ Notebook: Portable Entrepreneur (6fd9a6b5-5065-4c0f-ba7e-711c712fc3c9)       │
│ Total Sources: 288                                                           │
│ YouTube Video Sources: 288                                                   │
│                                                                              │
│ Total Estimated Tokens: ~1,296,000                                           │
│ Estimated Gemini Flash Input Cost: $0.0972 USD                               │
│ Estimated Synthesis Output Cost: $0.0432 USD                                 │
│ Total Estimated Cost: $0.1404 USD                                            │
╰──────────────────────────────────────────────────────────────────────────────╯

Domain Topic Clusters:
• Lead Generation & Client Acquisition (107 videos)
• Web Design & Agency Operations (105 videos)
• SEO & Local Search (28 videos)
• Solo Operator Systems & Productivity (28 videos)
• Client Retention, Onboarding & QBRs (11 videos)
• Pricing & Proposal Strategy (9 videos)
```

### 2. Run full distillation into Domain Compendiums (`run`)

```bash
notebook-alchemy run -n "Portable Entrepreneur"
```

This batches large clusters into indexed volumes (e.g. Vol. 1, Vol. 2 for 50+ video domains), generates rich Markdown reference notes with YAML frontmatter, executive summaries, SOPs, and cross-domain links in `./vault/compendiums/`, and generates a master `INDEX.md`.

---

## CLI Reference

| Command | Description |
| :--- | :--- |
| `notebook-alchemy test` | Automated self-test verifying catalog, clustering, synthesis & vault |
| `notebook-alchemy dry-run -n <name>` | Preview source counts, domain clusters, and token cost |
| `notebook-alchemy run -n <name>` | Distill hundreds of video sources into vault Domain Compendiums |
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
