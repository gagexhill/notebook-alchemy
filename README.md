# ⚗ Notebook Alchemy — YouTube channels, distilled for NotebookLM

> A NotebookLM notebook holds a few dozen sources and a channel holds hundreds of videos. Group the videos by subject, write one document per subject, and the archive fits.

<br>

<div align="center">

![Notebook Alchemy](https://img.shields.io/badge/Notebook%20Alchemy-0.3.0-5B3FD0?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Click](https://img.shields.io/badge/Click-8.1%2B-4EAA25?style=for-the-badge)
![Rich](https://img.shields.io/badge/Rich-13%2B-FF6F61?style=for-the-badge)
![yt-dlp](https://img.shields.io/badge/yt--dlp-2026.10%2B-FF0000?style=for-the-badge&logo=youtube&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini-3.8%20Flash-4285F4?style=for-the-badge&logo=googlegemini&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

**Maintainer:** [gagexhill](https://github.com/gagexhill) — open source, MIT licensed
**Runtime:** A Python command-line tool on your machine; the Gemini API and Google NotebookLM only when you opt in
**Status:** Alpha — [audit remediation #16](https://github.com/gagexhill/notebook-alchemy/issues/16) owns current progress

<br>

🧭 **[Operations](OPERATIONS.md)** &nbsp;|&nbsp; 📋 **[Issues](https://github.com/gagexhill/notebook-alchemy/issues)** &nbsp;|&nbsp; 🤝 **[Contributing](CONTRIBUTING.md)**

</div>

---

## 📌 Table of Contents

- [Project Overview](#-project-overview)
- [The Problem It Solves](#-the-problem-it-solves)
- [Core Concept: Distill by Topic](#-core-concept-distill-by-topic)
- [Run Modes](#-run-modes)
- [Tech Stack](#-tech-stack)
- [Project Architecture](#-project-architecture)
- [Key Features](#-key-features)
- [How It Works — Under the Hood](#-how-it-works--under-the-hood)
- [Getting Started](#-getting-started)
- [File Structure](#-file-structure)
- [Concepts & References](#-concepts--references)

---

## 🌐 Project Overview

**Notebook Alchemy** reads a YouTube channel, playlist or video, sorts its videos into topics, writes one Markdown document per topic, and uploads them to a Google NotebookLM notebook.

The documents come in two forms. With a Gemini API key, each one is a summary written from the spoken captions of its videos, citing them by number. Without one, each is a video list of titles and links that says plainly it contains no summary. Nothing is ever filled in with invented text, and the bundled sample catalog is used only when you pass `--mock`.

Everything runs on your machine. The tool keeps no account data beyond what a feature needs (see [What It Stores](#what-it-stores)), sends nothing anywhere except YouTube, the Gemini API and NotebookLM, and has no telemetry.

---

## 🔍 The Problem It Solves

A NotebookLM notebook accepts a limited number of sources, and the limit depends on your plan (Google's plan limits page lists it). A channel with hundreds of videos does not fit as one source per video, and importing only the first ones that fit leaves the rest of the channel out.

Even when the videos fit, each one arrives as its own source. A question about one subject has to draw on dozens of them, and there is no document that covers the subject as a whole.

Notebook Alchemy changes the unit. The videos are grouped by subject first, then written as one document per group, with a catalog of every source and an index. A 52-video channel becomes six or seven topic documents plus an overview and a catalog: eight or nine sources. The example under [Getting Started](#-getting-started) shows the real output.

---

## 🧬 Core Concept: Distill by Topic

```text
YouTube URL ──► find ──► sort ──► read captions ──► write documents ──► folder ──► NotebookLM
              yt-dlp     Gemini or  local files, cache,  Gemini summary    _INDEX.md  file sources,
              metadata   keywords   captions API         or video list     + catalog  replace by title
```

1. **Find.** `yt-dlp` reads flat metadata (id, title, duration), so no video is downloaded.
2. **Sort.** With a Gemini key, topics are named from a sample of titles and every title is then assigned by number; anything unassigned lands in a visible Miscellaneous topic. Without a key, a keyword table sorts the titles.
3. **Read captions** (with a Gemini key). Each video's text comes from your own `.txt`, `.vtt` or `.srt` files, then the local cache, then YouTube captions, fetched with a bounded worker pool. Videos with no captions are remembered for a week.
4. **Write documents.** A topic with more than `--max-per-vol` videos (50 by default) is divided into named subtopics, so each NotebookLM source says what it covers ("Federal Proposal Writing: Pricing Strategies", not "Vol 7"). With Gemini the subtopics come from that topic's own titles; without it, from the words that set its titles apart. A summary is written by Gemini and ends with a table of its sources; without Gemini, or without captions, the document is a video list.
5. **Save the folder.** Everything is written to a staging folder and swapped into `vault/<source>/compendiums/` only when complete, together with `_INDEX.md` and `_MASTER_SOURCES_CATALOG.md`. The leading underscore keeps those two at the top of NotebookLM's source list (sorted by title) and of most file browsers.
6. **Upload.** Each file goes to NotebookLM as a file source. The tool waits for NotebookLM to process each one, corrects any title NotebookLM rewrites to the file name, replaces older copies with the same title, removes sources it uploaded last time that this build no longer has, and reads the notebook back to confirm each title appears once. Any file that fails is listed with its cause.

---

## 🎛 Run Modes

| Mode | Command | Calls out to | Writes | Use it when |
|---|---|---|---|---|
| Preview | `notebook-alchemy dry-run <url>` | YouTube; Gemini if a key is set | Nothing | You want video count, topics and estimated cost first |
| Write with Gemini | `notebook-alchemy run <url> --no-sync` | YouTube, Gemini, YouTube captions | `vault/` | You want summaries to read or edit before uploading |
| Write offline | `notebook-alchemy run <url> --no-sync --offline` | YouTube only | `vault/` | You want video lists, the overview and the catalog with no API cost |
| Write and upload | `notebook-alchemy run <url>` | The above plus NotebookLM | `vault/`, one notebook | You are ready to use the result in NotebookLM |
| Upload later | `notebook-alchemy sync -n <name>` | NotebookLM | One notebook | Documents already exist in `vault/` |
| Add one video | `notebook-alchemy append -n <name> --title … --url …` | Gemini, only if a key is set and `--topic` is not given | The catalog and one document | A new video appeared and rewriting everything is not worth it |
| Try it | `notebook-alchemy run --mock --no-sync` | Nothing | `vault/` | You want to see the output before pointing it at a channel |
| Self-test | `notebook-alchemy test` | Nothing | A temporary folder | You want to check that everything installed correctly |
| Set up | `notebook-alchemy setup` | `notebooklm` login, only if you sign in | `.env` | You want to set the Gemini key, the upload account or the documents folder; run it again to change any of them |

---

## 🛠 Tech Stack

| Technology | Version | Role |
|---|---|---|
| Python | >=3.11 | Runtime |
| Click | >=8.1.0 | Command-line parsing, prompts, help |
| Rich | >=13.0.0 | Tables, progress bars, colour |
| httpx | >=0.27, <1 | Gemini API calls |
| yt-dlp[default] | >=2026.10.21 | Flat channel and playlist metadata; `[default]` adds the JavaScript solver YouTube requires |
| youtube-transcript-api | >=1.2.4, <2 | Captions when no local transcript exists |
| notebooklm-py | >=0.8.6, <0.9 | The `notebooklm` command used for login, notebooks and uploads |
| Gemini API | `gemini-3.8-flash` by default, `gemini-3.5-flash-lite` as fallback | Naming topics, sorting titles, writing summaries |
| pytest, ruff, pip-audit | dev extras | Tests, lint and format, dependency audit |

Versions are the ranges declared in `pyproject.toml`; the upper bounds keep out a next major release until it is tested. Override the model with `ALCHEMY_GEMINI_MODEL`.

---

## 🏗 Project Architecture

```text
src/notebook_alchemy/
├── cli.py          # Commands, options, prompts, exit codes
├── ui.py           # Steps, progress bars, warnings and errors, detail behind -v, markup-safe text
├── engine.py       # Orchestrates discovery, build, vault write, sync, append
├── analyzer.py     # Source discovery, domain clustering, token and cost estimates
├── channel.py      # YouTube scanning, URL handling, transcript resolution and cache
├── synthesizer.py  # Gemini summaries, video lists, overview and catalog files
├── gemini.py       # Gemini client: header auth, retries, model fallback, scrubbed errors, price table
├── client.py       # NotebookLM through the `notebooklm` command, with checked results
├── vault.py        # Vault layout and the atomic directory swap
├── config.py       # Environment and .env settings
├── errors.py       # Typed errors that carry a next action
└── mock_data.py    # Synthetic sample catalog, used only with --mock
```

`cli.py` builds an `AlchemyEngine` from `AlchemyConfig` and passes `ui.Reporter` as a progress callback. The engine asks `NotebookAnalyzer` for a `DryRunReport` (videos, domains, cost), has `Synthesizer` build each volume, and hands the staging folder to `VaultManager`. Upload goes through `NotebookLMClient`. Every failure is an `AlchemyError`; `ui.handle_errors` prints its message and next step and sets the exit code.

The code keeps its original names: a topic is a *domain*, a document a *compendium*, a part a *volume*, and the documents folder the *vault*. Everything a user reads (terminal output, help, the documents themselves and this guide outside the code) uses the everyday words.

---

## ✨ Key Features

- **Real input, real output** — a bad URL, an unknown notebook or a zero-video channel is an error with a next step. Sample data appears only behind `--mock`, with a warning that says so.
- **Checked uploads** — files upload a few at a time, each result is verified, failures are listed by file, and the exit code is non-zero.
- **Replace, not duplicate** — a re-run replaces same-titled sources and removes the ones it uploaded last time that are gone, so repeated runs do not pile up in the notebook. Sources you added yourself are never touched.
- **Survives a busy model** — if the default Gemini model is overloaded, retired, or out of daily quota, the run moves to the fallback model and says so.
- **Every title assigned** — sorting covers the whole channel, not a sample, and reports what it could not place.
- **Knows your plan's limit** — `run` and `sync` read the upload account's NotebookLM plan (for example Pro: 300 sources per notebook) and stop before building or uploading what cannot fit; when the notebook is tight, the copies being replaced are removed before the new ones go in. A limit that cannot be read is shown as unknown, never guessed.
- **Sources named by subject** — a large topic becomes named subtopics, and source titles keep the topic's own spelling ("SAM.gov", not "SAMgov").
- **Honest offline mode** — without a key you get video lists, an overview and a catalog, labelled as such. A document whose videos have no reachable captions (YouTube sometimes stops sharing captions for a while) is written as a video list too, not a summary invented from titles, and uses no Gemini request.
- **Safe writes** — output is kept per source, written to a staging folder and swapped in whole, so a failed run leaves the previous documents intact.
- **A known upload account** — `status`, `run` and `sync` name the Google account that receives uploads, or say plainly when the `notebooklm` profile did not record one. The first upload to an account asks first (`--yes` skips the question), and asks again if the account behind a profile changes. An account that cannot be identified is asked about on every upload.
- **Key hygiene** — the Gemini key travels in a header only and is removed from every error and log line.
- **Cost and quota before commitment** — `dry-run` estimates tokens from video length, prices them for the configured model (with Google's scheduled price changes), and prints how many Gemini requests a run needs. After writing, it shows what Gemini actually cost; `-v` adds the token counts Google reported.
- **Private by default** — no telemetry; nothing goes anywhere but YouTube, the Gemini API and NotebookLM; the upload confirmation keeps a salted hash of the account email, not the email; `clean --all` removes its caption cache, documents and records (`.env` and the `notebooklm` sign-in stay; the table under [What It Stores](#what-it-stores) says how to remove them).
- **Plain output, detail on request** — modelled on Google's `gcloud`: everyday words and one line per result by default, names in `[brackets]`, `Warning:` and `Error:` on stderr with a next step written as a command, questions asked as `Do you want to continue (y/N)?`, and paths, token counts and models behind `-v` (before or after the command). Progress bars on a terminal, and `NO_COLOR` honoured.
- **Hermetic tests** — the suite runs in a temporary working directory and home, with real processes and sockets blocked.

---

## ⚙ How It Works — Under the Hood

### Swapping a build into place

The documents folder is replaced as a whole. The old folder is renamed aside, the staged one renamed in, and the old one restored if either step fails.

```python
try:
    if had_old:
        final.rename(backup)
    staged.rename(final)
except OSError as exc:
    if had_old and backup.exists() and not final.exists():
        backup.rename(final)
    raise VaultError(
        f"Could not write the vault directory {final}: {exc}",
        hint="Close programs that have files in it open (editors, sync tools) and retry.",
    ) from exc
```

### Keeping the API key out of everything

The key goes in a request header, never the URL, and every message passes through `scrub` before it is raised or logged.

```python
headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
```

```python
def scrub(text: str, api_key: str) -> str:
    """Remove the API key (and any `key=...` query fragment) from text."""
    cleaned = _KEY_IN_TEXT.sub(r"\1***", text)
    if api_key:
        cleaned = cleaned.replace(api_key, "***")
    return cleaned
```

### Assigning every title by number

The model answers with title numbers, not title text, so a paraphrased title cannot drift. Anything outside the batch or the domain list is ignored and ends up in the Miscellaneous topic.

```python
for key, value in mapping.items():
    try:
        t_idx, d_idx = int(key), int(value)
    except (TypeError, ValueError):
        continue
    if start <= t_idx < start + len(batch) and 0 <= d_idx < len(domains):
        assigned[t_idx] = d_idx
```

### Uploading as a file

NotebookLM receives each document as a file. A document can be larger than one command-line argument can carry on Windows, so the content never travels there.

```python
safe = re.sub(r"[^\w\-]+", "_", document.title).strip("_") or "source"
# A scanner briefly holding the temp file must not turn a finished upload into an error.
with tempfile.TemporaryDirectory(prefix="notebook-alchemy-", ignore_cleanup_errors=True) as tmp:
    path = Path(tmp) / f"{safe[:80]}.md"
    path.write_text(document.content, encoding="utf-8")
    data = self._run(
        [
            "source",
            "add",
            str(path),
            "-n",
            notebook_id,
            "--type",
            "file",
            "--title",
            document.title,
            "--request-timeout",
            f"{UPLOAD_HTTP_TIMEOUT:g}",
        ],
        timeout=UPLOAD_TIMEOUT,
    )
```

---

## 🚀 Getting Started

### Prerequisites

- Python 3.11 or newer
- A Google account with NotebookLM, only if you want to upload
- A [Gemini API key](https://aistudio.google.com/app/apikey), only if you want summaries

### Installation

```bash
git clone https://github.com/gagexhill/notebook-alchemy.git
cd notebook-alchemy
python -m pip install -e .
notebook-alchemy test
```

`notebook-alchemy test` writes the sample catalog to a temporary folder and needs no network or credentials. Use `python3` on macOS and Linux if `python` is not Python 3, and run any command as `python -m notebook_alchemy.cli …` if `notebook-alchemy` is not on your `PATH`.

### First run, no accounts needed

Preview a channel, then write it offline into `vault/` (video lists, an overview and a catalog, no API cost):

```bash
notebook-alchemy dry-run https://www.youtube.com/@Hyperplexed
notebook-alchemy run https://www.youtube.com/@Hyperplexed --no-sync --offline
```

### Add Gemini and NotebookLM

The guided setup asks for the Gemini key (hidden as you type), the Google account to upload to and the documents folder, and writes them to `.env`. Run it again whenever you want to change one:

```bash
notebook-alchemy setup
notebook-alchemy status
```

The Gemini key and the NotebookLM account are separate credentials and can belong to different Google accounts. The key bills to the Google Cloud project it was created in. Uploads go to whichever account the `notebooklm` profile is signed in to; `status` names it. A Google AI subscription plan does not change Gemini API quota or billing: Google's [Google AI plans page](https://ai.google.dev/gemini-api/docs/google-ai-plans) says plan benefits apply only inside the AI Studio web interface and API-key usage is billed separately.

The simplest sign-in reuses a browser you are already signed in to Google with; to pick one of several accounts there, name it, and keep it in its own profile:

```bash
notebook-alchemy login --browser-cookies chrome
notebooklm auth inspect --browser chrome
notebook-alchemy login --browser-cookies chrome --account you@example.com --profile personal
```

`notebook-alchemy login` on its own opens a sign-in window instead; it needs a browser for Playwright, installed once with `python -m playwright install chromium`. Set `ALCHEMY_NOTEBOOKLM_PROFILE=personal` in `.env` (setup does this), or pass `--profile personal` to `run`, `sync`, `status` and `login`.

Then write and upload:

```bash
notebook-alchemy run https://www.youtube.com/@Hyperplexed
```

A real run on that channel, recorded from the terminal (the first is the preview, the second writes and uploads):

![Terminal output of a dry run of the Hyperplexed channel](docs/images/dry-run.svg)

![Terminal output of a full run that writes and uploads the Hyperplexed channel](docs/images/run.svg)

The cost estimate counts only the output you can see; Gemini 3.x models also think, and thinking tokens bill as output, so the actual cost shown after writing is higher. `ALCHEMY_GEMINI_MODEL` selects a cheaper model such as `gemini-3.5-flash-lite`.

A new AI Studio key starts on Google's free tier, which limits requests per day for each model. `dry-run` prints how many requests a run needs; when a limit is reached the run continues on the fallback model, and a large channel needs billing enabled on the key's project.

Keyword grouping is the fallback when no key is set. It cannot tell what an unusual title is about, so some videos land in "General / Specialized Topics"; with a Gemini key the topics are named from the channel's own titles. Topics are named again on each run, so their names can differ between runs.

A channel URL reads the channel's Videos and Live tabs; add Shorts with `--tabs videos,streams,shorts`, or read only one tab with `--tabs videos`. Playlist and video URLs are read as they are.

Captions are fetched in English first; set `ALCHEMY_CAPTION_LANGUAGES` (for example `de,en`) for other languages. Without a match, the uploader's own captions are used in any language, then automatic ones.

### What It Stores

Everything stays on your machine. `notebook-alchemy clean --all` removes the caption cache, the documents and the records, and asks before deleting documents or records; the last two rows are removed as shown.

| Where | What | Removed by |
|---|---|---|
| `.env` in the folder you run from | Your Gemini key, profile and documents folder, written by `setup` | Delete the file |
| `vault/<source>/compendiums/` | The documents, overview and catalog | `clean --vaults` |
| `~/.notebook-alchemy/cache/` | Downloaded captions, and which videos had none (for a week) | `clean --cache` |
| `~/.notebook-alchemy/sync/` | Per notebook id, the titles the last upload added, so the next can remove stale ones | `clean --records` |
| `~/.notebook-alchemy/upload_accounts.json` | Per profile, a salted hash of the account you confirmed uploads to (the email is not stored, though a known email can be checked against it) | `clean --records` |
| `~/.notebooklm/profiles/<profile>/` | The Google sign-in, kept by the `notebooklm` command | `notebooklm auth logout` |

### Build for Production

The package builds a wheel with Hatch:

```bash
python -m pip install build
python -m build
```

> **Notes.** `dry-run` and `run` show an estimated cost; the estimate comes from video length and the price table in `src/notebook_alchemy/gemini.py`, so check Google's pricing page before a large run. `clean` deletes only this tool's own files, and asks before deleting documents or records.

For contributors: [CONTRIBUTING.md](CONTRIBUTING.md) to propose a change, [AGENTS.md](AGENTS.md) for the rules every change keeps, [OPERATIONS.md](OPERATIONS.md) for the maintainer runbook, [DEVLOG.md](DEVLOG.md) for past design decisions, and [the issues](https://github.com/gagexhill/notebook-alchemy/issues) for current work.

---

## 📁 File Structure

```text
notebook-alchemy/
├── src/notebook_alchemy/   # The tool (see Project Architecture)
├── tests/                  # Hermetic pytest suite; conftest.py isolates every test
├── prompts/                # AI Studio blueprint that mirrors the synthesis request
├── .github/                # Issue and PR templates, Dependabot, validate workflow
├── .githooks/              # pre-push secret scan (gitleaks)
├── scripts/                # Regenerate the README recordings and the social preview
├── docs/images/            # README recordings and the social preview image
├── AGENTS.md               # Rules every change keeps (CLAUDE.md points here)
├── DEVLOG.md               # Past design decisions and why
├── OPERATIONS.md           # Maintainer runbook
├── SECURITY.md             # Credential handling and private reporting
├── CONTRIBUTING.md         # Setup, checks, pull requests
├── pyproject.toml          # Package, dependencies, ruff and pytest settings
├── .env.example            # Environment variables, placeholders only
└── LICENSE                 # MIT
```

---

## 📚 Concepts & References

| Concept | Applied In |
|---|---|
| Flat metadata extraction | `src/notebook_alchemy/channel.py` (`ChannelScanner.scan`) |
| Two-stage LLM clustering with index-based assignment | `src/notebook_alchemy/analyzer.py` (`_cluster_with_gemini`) |
| Keyword fallback with word-start boundaries | `src/notebook_alchemy/analyzer.py` (`KEYWORD_DOMAINS`) |
| WebVTT and SRT parsing | `src/notebook_alchemy/channel.py` (`parse_captions`) |
| Negative caching with expiry | `src/notebook_alchemy/channel.py` (`TranscriptManager`) |
| Retry with backoff and `Retry-After` | `src/notebook_alchemy/gemini.py` (`GeminiClient.generate`) |
| Model fallback after overload, retirement or quota | `src/notebook_alchemy/gemini.py` (`GeminiClient.generate`) |
| Read-back verification after an upload | `src/notebook_alchemy/engine.py` (`sync_to_notebooklm`) |
| Identity read from the credential's own record, never guessed | `src/notebook_alchemy/client.py` (`upload_account`) |
| Consent recorded per profile and account | `src/notebook_alchemy/client.py` (`upload_confirmed`), `src/notebook_alchemy/cli.py` (`_confirm_first_upload`) |
| Header-based API authentication and secret scrubbing | `src/notebook_alchemy/gemini.py` (`scrub`) |
| Atomic directory replacement | `src/notebook_alchemy/vault.py` (`replace_dir`) |
| Typed errors that carry a next action | `src/notebook_alchemy/errors.py`, `src/notebook_alchemy/ui.py` |
| Hermetic tests with blocked processes and sockets | `tests/conftest.py` |
| Scripted HTTP fakes | `tests/test_gemini.py` (`httpx.MockTransport`) |
| Blueprint kept in step with code | `tests/test_prompts.py` |

---

<div align="center">

**Notebook Alchemy** — *a channel in, a notebook out.*

© 2026 gagexhill and contributors · MIT License

</div>
