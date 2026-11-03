# notebook-alchemy

[![CI](https://github.com/gagexhill/notebook-alchemy/actions/workflows/ci.yml/badge.svg)](https://github.com/gagexhill/notebook-alchemy/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A CLI tool to bypass Google NotebookLM's 300-source limit.

If you add an entire YouTube playlist or channel backlog to NotebookLM, you will hit the hard 300-source cap immediately. NotebookLM slows down, chat context gets fragmented, and you run out of slots for any new notes or documents.

`notebook-alchemy` solves this: it fetches the transcripts, clusters related videos into coherent topics, and condenses hundreds of scattered video links into clean, structured reference notes directly inside your notebook.

```
288 raw YouTube links  ──►  notebook-alchemy  ──►  12 topic compendiums
(fills notebook to cap)                              (288 free slots remaining)
```

---

## Features

- **Bypass the 300-source wall**: Turn hundreds of video links into 10–15 clean reference notes.
- **Safe dry-run preview**: See video counts, topic clusters, and token cost estimates before touching anything in NotebookLM.
- **Cheap & fast**: Uses Gemini Flash for synthesis (~$0.15 total for hundreds of video transcripts).
- **Zero credential leaks**: Browser session cookies and API keys stay local and are strictly git-ignored.
- **Markdown vault export**: Optionally exports all notes to local markdown folders (Obsidian, Logseq, etc.).

---

## Quickstart

### Installation

```bash
git clone https://github.com/gagexhill/notebook-alchemy.git
cd notebook-alchemy
pip install -e .
```

### 1. Set your Gemini API key

Create a local `.env` file (this is strictly git-ignored):

```bash
cp .env.example .env
```

Add your Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey):

```env
GEMINI_API_KEY=your_key_here
```

### 2. Log in to NotebookLM

```bash
notebooklm login
```

This opens a browser window for a one-time Google login and saves your session locally to `~/.notebooklm/profiles/default/storage_state.json`.

---

## Usage

### Preview a notebook without making any changes

Run a dry run on any notebook by name or ID:

```bash
notebook-alchemy dry-run -n "Example Notebook"
```

Output:

```text
╭─────────────────────────── Dry-Run Cost & Token Sizing ───────────────────────────╮
│ Notebook: Example Notebook (00000000-0000-4000-8000-0000000000aa)             │
│ Total Sources: 288                                                                 │
│ YouTube Video Sources: 288                                                         │
│ Other Documents/Notes: 0                                                           │
│                                                                                    │
│ Total Estimated Tokens: ~1,296,000                                                 │
│ Estimated Gemini Flash Input Cost: $0.0972 USD                                     │
│ Estimated Synthesis Output Cost: $0.0432 USD                                       │
│ Total Estimated Cost: $0.1404 USD                                                  │
╰────────────────────────────────────────────────────────────────────────────────────╯

Topic Clusters Preview:
• Lead Generation & Client Acquisition (74 videos)
• Web Design & Agency Operations (62 videos)
• Client Retention, Onboarding & QBRs (41 videos)
• Pricing & Proposal Strategy (38 videos)
• SEO & Local Search (35 videos)
• Solo Operator Systems & Productivity (38 videos)
```

### Sync notes to a local markdown vault

```bash
notebook-alchemy sync --vault-path ./vault
```

---

## CLI Reference

| Command | Description |
| :--- | :--- |
| `notebook-alchemy status` | Check engine configuration and authentication status |
| `notebook-alchemy dry-run -n <name>` | Preview source counts, topic clusters, and token cost |
| `notebook-alchemy sync --vault-path <dir>` | Export notes to an Obsidian/Markdown vault |
| `notebooklm list` | List all NotebookLM notebooks in your account |
| `notebooklm source list -n <id>` | List all sources in a specific notebook |

---

## How It Works

1. **Source Inspection**: Reads your notebook's existing sources via the local session.
2. **Transcript Extraction**: Grabs full transcripts from YouTube sources.
3. **Topic Clustering**: Groups related videos into topical themes (e.g. all videos on pricing, all videos on SEO).
4. **Synthesis**: Uses Gemini Flash to compile each group into a comprehensive, cited reference note.
5. **Notebook Update**: Places the condensed reference notes into your notebook so your source count drops from 250+ down to ~12, freeing up room for more research while keeping full knowledge retention.

---

## Contributing

Pull requests are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for local development setup and guidelines.

```bash
# Run tests
python -m unittest discover tests

# Run linter
ruff check .
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
