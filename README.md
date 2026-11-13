# ⚗ Notebook Alchemy — YouTube channels, distilled for NotebookLM

> A NotebookLM notebook holds a few hundred sources at most and a channel holds hundreds of videos. Read every word, keep each point once, file it by topic, and the whole channel fits.

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
- [Core Concept: Every Word, Kept Once, Filed by Topic](#-core-concept-every-word-kept-once-filed-by-topic)
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

**Notebook Alchemy** takes a YouTube channel, playlist or video link, reads what every video says, keeps each point once, and uploads the result to a Google NotebookLM notebook as a few topic books. Each book can hold up to NotebookLM's 500,000 words per source.

The books hold the speakers' own words, never an AI rewrite:
- **Gemini's job:** it decides which passages are worth keeping, files each one under a topic and section, puts each section in teaching order, and marks information that has gone out of date.
- **Each passage names its source:** the video it came from and the date, so NotebookLM's answers, quizzes and Audio Overviews cite something you can check.
- **Without a Gemini key:** the books hold every video's cleaned words, sorted by title.

Everything runs on your machine. The books go straight to NotebookLM; nothing is kept unless you ask for a copy with `--save`. The tool keeps no account data beyond what a feature needs (see [What It Stores](#what-it-stores)), sends nothing anywhere except YouTube, the Gemini API and NotebookLM, and has no telemetry.

---

## 🔍 The Problem It Solves

A NotebookLM notebook accepts a limited number of sources: 50 on the free plan, 300 on Pro. A channel with hundreds of videos does not fit as one source per video. Importing only the first ones that fit leaves the rest of the channel out.

Even when the videos fit, each arrives as its own source, full of intros, sponsor reads and the same advice repeated across dozens of videos. Summaries are no better: NotebookLM answers from the text it is given, so a short summary leaves it little to answer from.

Notebook Alchemy changes the unit. Every video's words are read, repeated passages are kept once, and what remains is filed by subject into a few books. A channel of hundreds of videos becomes a handful of rich sources plus an overview.

---

## 🧬 Core Concept: Every Word, Kept Once, Filed by Topic

```text
channel ──► find ──► words ──► clean ──► label ──► sort ──► consolidate ──► books ──► NotebookLM
link       yt-dlp   captions,  filler,   Gemini,   topics,  order, dupes,    plain     temp file,
           + dates  yt-dlp,    repeats   cached    sections, out-of-date     Markdown  replace by title
                    NotebookLM removed   per video cached    cached
```

1. **Find.** `yt-dlp` reads every video's id, title, duration and date from the channel's listings; no video is downloaded.
2. **Words.** Each video's text comes from the first of these routes that works:
   - your own `.txt`, `.vtt` or `.srt` files, then the local cache;
   - YouTube's captions, spaced a few seconds apart;
   - yt-dlp's subtitle download;
   - NotebookLM, which fetches a video's transcript on Google's side and is not affected when YouTube refuses your connection.

   A refused route pauses while the others carry on. A video still missing is listed in the overview and retried on the next run. With `--watch N`, Gemini watches up to N of those videos itself and writes down what is said (paid: about $0.15 per 30 minutes; videos over 90 minutes are skipped); the overview names each video read this way.
3. **Clean.** On your machine, free: caption noise and filler go, short calls to action and sponsor sentences go, and a passage repeated across videos (or within one) is kept once, in the newest video.
4. **Label.** One Gemini request per video reads its numbered chunks and returns labels only: keep or drop, a headline, how long the information stays true, and an editor's note where a passage needs context. Captions without punctuation also come back punctuated; the punctuation is copied onto the original words, and no word is added or lost. Labels are cached per video.
5. **Sort.**
   - One request names the notebook and its topics and sections.
   - Every passage is placed by number; one the model does not place goes to a visible Miscellaneous book.
   - When new videos bring passages that fit nowhere, the map grows: new sections or topics are added, and nothing already placed moves. A section past 200 passages is divided into named subsections.
   - One request per section orders it, finds duplicates and finds passages a newer one replaces.
   - Old news is left out; old rules and figures are kept, marked "As of".
6. **Books and upload.** One plain-Markdown book per topic (split into parts only past 450,000 words; a section stays in its part from run to run), plus an `_OVERVIEW`. Each goes to NotebookLM as a file. The tool waits for processing, corrects any title NotebookLM changes, replaces earlier copies of books whose text changed (the rest are left as they are), removes books the last run made that this one no longer has, and reads the notebook back. A rerun updates the same notebook.

---

## 🎛 Run Modes

| Mode | Command | Calls out to | Writes | Use it when |
|---|---|---|---|---|
| Guided first run | `notebook-alchemy` | YouTube, NotebookLM, Gemini if you add a key | One notebook | You want to be asked for each choice instead of reading options |
| Preview | `notebook-alchemy dry-run <url>` | YouTube | Nothing | You want the video count, the words already read and the cost first |
| Build and upload | `notebook-alchemy run <url>` | YouTube, NotebookLM, Gemini if a key is set | One notebook | You want the books in NotebookLM |
| Keep a copy | `notebook-alchemy run <url> --save books` | YouTube, NotebookLM for blocked captions, Gemini | `books/` | You want the books as files instead of uploading |
| Read the unreadable | `notebook-alchemy run <url> --watch 10` | YouTube, NotebookLM, Gemini | One notebook | Some videos have no captions any route can read, and you accept Gemini's cost to have it watch them |
| Without AI | `notebook-alchemy run <url> --offline` | YouTube, NotebookLM | One notebook | You want every word, sorted by title, at no API cost |
| Try it | `notebook-alchemy run --mock --save books` | Nothing | `books/` | You want to see the output before pointing it at a channel |
| Self-test | `notebook-alchemy test` | Nothing | A temporary folder | You want to check that everything installed correctly |
| Keep a sign-in fresh (optional) | `notebook-alchemy keep-signed-in` (`--off` to stop) | NotebookLM, every 20 minutes | A scheduled task for your user | Your computer keeps a refreshed sign-in alive (`status` shows whether it does) |
| Move computers | `notebook-alchemy export-state FILE`, then `import-state FILE` | Nothing | A zip, then `~/.notebook-alchemy/` | You want to carry on from another computer without paying again |
| Set up | `notebook-alchemy setup` | `notebooklm` login, only if you sign in | `.env` | You want to set the Gemini key or the upload account; run it again to change either |

Run `run` again whenever the channel posts. Only new videos cost anything, new passages join the sections they belong to, and only the books that changed are uploaded again.

---

## 🛠 Tech Stack

| Technology | Version | Role |
|---|---|---|
| Python | >=3.11 | Runtime |
| Click | >=8.1.0 | Command-line parsing, prompts, help |
| Rich | >=13.0.0 | Tables, progress bars, colour |
| httpx | >=0.27, <1 | Gemini API calls, including batch mode |
| yt-dlp[default] | >=2026.10.21 | Channel listings with dates, and subtitles; `[default]` adds the JavaScript solver YouTube requires |
| youtube-transcript-api | >=1.2.4, <2 | Captions when no local transcript exists |
| notebooklm-py | >=0.8.6, <0.9 | The `notebooklm` command used for sign-in, notebooks, uploads and reading transcripts on Google's side |
| Gemini API | `gemini-3.8-flash` by default | Labelling passages, naming topics, sorting and ordering |
| pytest, ruff, pip-audit | dev extras | Tests, lint and format, dependency audit |

Versions are the ranges declared in `pyproject.toml`; the upper bounds keep out a next major release until it is tested. Override the model with `ALCHEMY_GEMINI_MODEL`.

---

## 🏗 Project Architecture

```text
src/notebook_alchemy/
├── cli.py          # Commands, options, prompts, exit codes
├── ui.py           # Steps, progress bars, warnings and errors, detail behind -v, markup-safe text
├── engine.py       # The pipeline, the NotebookLM transcript route, and the upload
├── channel.py      # YouTube scanning, dates, and every route to a video's words
├── cleanup.py      # Local cleanup, numbered chunks, passages repeated across videos
├── knowledge.py    # Gemini labels per video, cached; batch mode with a standard-call fallback
├── library.py      # Passages, date rules, topics and sections, consolidation, request cache
├── writer.py       # The topic books and the overview
├── offline.py      # The no-key path: every video's words, sorted by title words
├── state.py        # export-state / import-state: the working files, moved as a checked zip
├── keepalive.py    # keep-signed-in: the scheduled `notebooklm auth refresh`, per profile
├── gemini.py       # Gemini client: header auth, retries, batch mode, scrubbed errors, price table
├── client.py       # NotebookLM through the `notebooklm` command, with checked results
├── config.py       # Environment and .env settings
├── errors.py       # Typed errors that carry a next action
└── mock_data.py    # Synthetic sample catalog and transcripts, used only with --mock
```

`cli.py` builds an `AlchemyEngine` from `AlchemyConfig` and passes `ui.Reporter` as a progress callback. `AlchemyEngine.build` runs the pipeline:
- `TranscriptManager` gets the words;
- `cleanup` chunks and dedupes them;
- `Labeler` labels them;
- `LibraryBuilder` sorts and consolidates;
- `render_library` writes the books.

`AlchemyEngine.publish` uploads them through `NotebookLMClient`. Every failure is an `AlchemyError`; `ui.handle_errors` prints its message and next step and sets the exit code.

---

## ✨ Key Features

- **Every word, not a summary.** The books are the speakers' own words. Gemini only labels, files and orders them, so nothing is invented or flattened into a summary.
- **Kept once.** A passage repeated across videos (stock intros, pitches, the same advice in thirty videos) appears once, in the newest video, and names every video that said it.
- **Dated.** Every passage carries its video's date. Old news is left out; old rules and figures are marked "As of 2023: check the current rules"; the overview lists what was left out.
- **Captions even when YouTube refuses.**
  - When YouTube blocks your connection, NotebookLM reads the transcripts instead.
  - Requests are spaced so a long channel is not refused in the first place.
  - A refused route pauses without stopping the run.
- **Cheap to run again.**
  - Captions, labels, the topics, every passage's place and every section's order are cached by what produced them.
  - A rerun with no new videos asks Gemini nothing and uploads nothing.
  - New videos are placed into the existing topics; the map grows instead of being rebuilt, and only the books whose text changed are uploaded.
  - Gemini's batch mode (half price) is used when the key's project has billing.
  - A used-up daily quota stops cleanly and resumes the next day.
- **Nothing kept that you did not ask for.** The books go to NotebookLM and are not stored; `--save DIR` writes a copy.
- **Knows your plan's limit.** `run` reads the upload account's NotebookLM plan (for example Pro: 300 sources per notebook) and will not upload past it. When the notebook is tight, the copies being replaced are removed first.
- **A known upload account.** `status` and `run` name the Google account that receives uploads, or say plainly when it is unknown. The first upload to an account asks first (`--yes` skips the question).
- **Checked uploads.** Each book is uploaded, waited on, titled and read back; failures are listed by book and the exit code is non-zero.
- **Key hygiene.** The Gemini key travels in a header only and is removed from every error and log line.
- **Plain output, detail on request.** Modelled on Google's `gcloud`:
  - everyday words and one line per result;
  - names in `[brackets]`;
  - `Warning:` and `Error:` on stderr, with a next step written as a command;
  - paths, token counts and models behind `-v`.
- **Hermetic tests.** The suite runs in a temporary working directory and home, with real processes and sockets blocked.

---

## ⚙ How It Works — Under the Hood

### Labels by number, never rewritten text

Gemini answers with chunk numbers. A segment that points outside the video's chunks, or overlaps one already taken, is dropped and counted; a kept chunk no segment covers is kept anyway.

```python
span = {n for n in range(start, end + 1) if n in numbers}
if start > end or not span or span & taken:
    invalid += 1
    continue
taken |= span
```

### Punctuation without changing a word

For captions with no punctuation, the label answer also carries the kept words punctuated. Its punctuation and capitals are copied onto the original words wherever the two line up; a word the model dropped keeps its original form, and a word it added is never used.

```python
for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
    if tag == "equal":
        for k in range(i2 - i1):
            out[i1 + k] = pun[j1 + k]  # same word, with the model's punctuation and capitals
```

### Keeping the API key out of everything

The key goes in a request header, never the URL, and every message passes through `scrub` before it is raised or logged.

```python
headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
```

### Uploading as a file

NotebookLM receives each book as a file. A book can be far larger than one command-line argument can carry on Windows, so the content never travels there.

```python
with tempfile.TemporaryDirectory(prefix="notebook-alchemy-", ignore_cleanup_errors=True) as tmp:
    path = Path(tmp) / f"{safe[:80]}.md"
    path.write_text(document.content, encoding="utf-8")
```

---

## 🚀 Getting Started

### Prerequisites

- Python 3.11 or newer
- A Google account with NotebookLM. It is also how blocked videos' transcripts are read.
- A [Gemini API key](https://aistudio.google.com/app/apikey), only if you want AI sorting

### Installation

```bash
git clone https://github.com/gagexhill/notebook-alchemy.git
cd notebook-alchemy
python -m pip install -e .
notebook-alchemy test
```

`notebook-alchemy test` builds the sample catalog in a temporary folder and needs no network or credentials. Use `python3` on macOS and Linux if `python` is not Python 3, and run any command as `python -m notebook_alchemy.cli …` if `notebook-alchemy` is not on your `PATH`.

### Set up and run

The quickest start is the guided run: type `notebook-alchemy` with nothing after it. It signs you in to NotebookLM (a browser window), offers a free Gemini key for topic sorting, asks for a channel link, and builds the notebook. Every question has a default, so Enter is enough.

```bash
notebook-alchemy
```

For more control, the separate commands below do the same steps one at a time.

The guided setup asks for the Gemini key (hidden as you type) and the Google account to upload to, and writes them to `.env`. Run it again whenever you want to change either:

```bash
notebook-alchemy setup
notebook-alchemy status
```

The Gemini key and the NotebookLM account are separate credentials and can belong to different Google accounts:
- **The key** bills to the Google Cloud project it was created in.
- **Uploads** go to whichever account the `notebooklm` profile is signed in to; `status` names it.
- **A Google AI subscription** does not change Gemini API quota or billing. Google's [Google AI plans page](https://ai.google.dev/gemini-api/docs/google-ai-plans) says plan benefits apply only inside the AI Studio web interface, and API-key usage is billed separately.

The simplest sign-in reuses a browser you are already signed in to Google with. To pick one of several accounts there, name it and keep it in its own profile:

```bash
notebook-alchemy login --browser-cookies chrome
notebook-alchemy login --browser-cookies chrome --account you@example.com --profile personal
```

`notebook-alchemy login` on its own opens a sign-in window instead; it needs a browser for Playwright, installed once with `python -m playwright install chromium`.

Google ends a sign-in that sits idle for a few hours. When yours has ended, `run` and the guided run say so, ask you to sign in again (one browser window), and carry on; the work already done is kept. A long run checks again before it uploads. Outside a terminal, the run stops with the sign-in command instead.

`notebook-alchemy keep-signed-in` is optional: it refreshes the sign-in every 20 minutes in the background (a Task Scheduler task on Windows, a launchd agent on macOS, a systemd user timer or cron on Linux). It keeps a sign-in alive only while Google accepts the refresh, and on some computers Google still ends it within hours; `status` shows when it last worked. `keep-signed-in --off` removes it. Notebook Alchemy never stores a long-lived Google token for you.

Then preview and build:

```bash
notebook-alchemy dry-run https://www.youtube.com/@Hyperplexed
notebook-alchemy run https://www.youtube.com/@Hyperplexed
```

A real preview of that channel, recorded from the terminal:

![Terminal output of a dry run of the Hyperplexed channel](docs/images/dry-run.svg)

**Cost and quota**
- **Per video:** one labelling request. The label answer is a few percent of the transcript; it is larger only for captions that need punctuation.
- **Estimate first:** `dry-run` estimates the cost from the words already read and from video lengths.
- **Free tier:** a new AI Studio key starts on Google's free tier, which limits requests per day. A large channel then takes a few runs on different days; each picks up where the last stopped.
- **Billing:** with billing enabled on the key's project, batch mode halves the cost.

**Captions**
- **Language:** captions are read in English first. Set `ALCHEMY_CAPTION_LANGUAGES` (for example `de,en`) for other languages.
- **When YouTube refuses:** NotebookLM reads the transcripts in a working notebook called "Notebook Alchemy workspace" in your upload account. Each video is added there, read and removed.
- **Browser sign-in for yt-dlp:** `--cookies chrome` lets yt-dlp use your browser's YouTube sign-in. On Windows, Chrome and Edge often lock or encrypt it, and the run says so.

**Channel tabs:** a channel link reads the Videos and Live tabs; add Shorts with `--tabs videos,streams,shorts`. Playlist and video links are read as they are.

### What It Stores

Everything stays on your machine. `notebook-alchemy clean --all` removes the working files and asks first; the last two rows are removed as shown.

| Where | What | Removed by |
|---|---|---|
| `.env` in the folder you run from | Your Gemini key and profile, written by `setup` | Delete the file |
| `~/.notebook-alchemy/cache/` | Downloaded words per video, and which videos had none (for a week) | `clean --cache` |
| `~/.notebook-alchemy/knowledge/`, `library/` | Gemini's labels, topics, placements and section orders, so reruns cost nothing | `clean --labels` |
| `~/.notebook-alchemy/sync/`, `notebooks.json`, `upload_accounts.json`, `batch.json` | What the last upload added, which notebook each channel uses, a salted hash of the account you confirmed uploads to (not the email), and a running batch job | `clean --records` |
| `~/.notebooklm/profiles/<profile>/` | The Google sign-in, kept by the `notebooklm` command | `notebooklm auth logout` |
| Your system scheduler, and `~/.notebook-alchemy/keepalive.json` | The 20-minute sign-in refresh (only if you turned it on) and when it last ran | `keep-signed-in --off`; `clean --records` |
| Your NotebookLM account | The notebook itself, and the "Notebook Alchemy workspace" notebook used to read blocked videos (left empty after each run) | Delete them in NotebookLM |

`notebook-alchemy export-state FILE` copies the cache, `knowledge/`, `library/`, `sync/` and `notebooks.json` into one zip; `import-state FILE` on another computer carries on from there. Sign-ins, the key and the upload confirmation are not included.

Version 0.2 wrote a `vault/` folder; `clean --vaults` removes it.

### Build for Production

The package builds a wheel with Hatch:

```bash
python -m pip install build
python -m build
```

> **Notes.** `dry-run` shows an estimated cost; the estimate comes from transcript length and the price table in `src/notebook_alchemy/gemini.py`, so check Google's pricing page before a large run. `clean` deletes only this tool's own files, and asks before deleting labels or records.

For contributors: [CONTRIBUTING.md](CONTRIBUTING.md) to propose a change, [AGENTS.md](AGENTS.md) for the rules every change keeps, [OPERATIONS.md](OPERATIONS.md) for the maintainer runbook, [DEVLOG.md](DEVLOG.md) for past design decisions, and [the issues](https://github.com/gagexhill/notebook-alchemy/issues) for current work.

---

## 📁 File Structure

```text
notebook-alchemy/
├── src/notebook_alchemy/   # The tool (see Project Architecture)
├── tests/                  # Hermetic pytest suite; conftest.py isolates every test
├── prompts/                # AI Studio blueprint that mirrors the label request
├── .github/                # Issue and PR templates, Dependabot, validate workflow
├── .githooks/              # pre-push secret scan (gitleaks)
├── scripts/                # Regenerate the README recording and the social preview
├── docs/images/            # README recording and the social preview image
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
| Flat metadata extraction with approximate dates | `src/notebook_alchemy/channel.py` (`ChannelScanner.scan`) |
| Per-route pauses and paced requests | `src/notebook_alchemy/channel.py` (`TranscriptManager`) |
| Reading transcripts on Google's side | `src/notebook_alchemy/engine.py` (`transcripts_via_notebooklm`) |
| Shingle-based duplicate removal, newest copy kept | `src/notebook_alchemy/cleanup.py` (`mark_duplicates`) |
| Labels by number instead of generated text | `src/notebook_alchemy/knowledge.py` (`parse_labels`) |
| Content-keyed caching of every paid answer | `src/notebook_alchemy/knowledge.py` (`label_key`), `src/notebook_alchemy/library.py` (`RequestCache`) |
| Batch mode with a standard-call fallback | `src/notebook_alchemy/knowledge.py` (`Labeler`), `src/notebook_alchemy/gemini.py` (`submit_batch`) |
| Date rules for time-sensitive information | `src/notebook_alchemy/library.py` (`apply_date_rules`) |
| Read-back verification after an upload | `src/notebook_alchemy/engine.py` (`publish`) |
| Identity read from the credential's own record, never guessed | `src/notebook_alchemy/client.py` (`upload_account`) |
| Header-based API authentication and secret scrubbing | `src/notebook_alchemy/gemini.py` (`scrub`) |
| Typed errors that carry a next action | `src/notebook_alchemy/errors.py`, `src/notebook_alchemy/ui.py` |
| Hermetic tests with blocked processes and sockets | `tests/conftest.py` |
| Blueprint kept in step with code | `tests/test_prompts.py` |

---

<div align="center">

**Notebook Alchemy** — *a channel in, a notebook out.*

© 2026 gagexhill and contributors · MIT License

</div>
