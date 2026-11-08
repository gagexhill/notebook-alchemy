# AGENTS.md

Rules for anyone changing this repository, people and coding agents alike. `CONTRIBUTING.md` covers how
to propose a change; this file covers what a change must keep true.

## Orientation

- `README.md` is the user guide. `OPERATIONS.md` is the maintainer runbook. `DEVLOG.md` records
  completed design decisions and why. Open work is a GitHub issue, never a document or an inline comment.
- Source is `src/notebook_alchemy/`, in pipeline order:
  - `channel.py`: YouTube scanning and every route to a video's words.
  - `cleanup.py`: local cleanup, chunks and dedupe.
  - `knowledge.py`: Gemini labels, cached per video.
  - `library.py`: passages, topics, sections, consolidation and dates.
  - `writer.py`: the topic books and overview.
  - `engine.py`: the pipeline and the upload.
  - `cli.py`: commands.
  - `ui.py`: output.
  - `client.py`: NotebookLM through the `notebooklm` CLI.
  - `gemini.py`: the Gemini API, including batch mode.
  - `offline.py`: the no-key path.
  - `config.py`: settings.
  - `errors.py`: typed errors.
  - `mock_data.py`: the sample catalog behind `--mock`.

## Invariants

- Real input never becomes sample data. Sample data appears only behind an explicit `--mock`, is
  labelled in every output, and is never uploaded.
- A success message is printed only after the work is confirmed: check return codes, parse results, and
  raise a typed error from `errors.py` with a message and a next action. No broad `except` that discards
  an outcome.
- The Gemini key goes in the `x-goog-api-key` header only. Never log, print or store it outside `.env`,
  and never put it in a URL. Every raised message passes through `gemini.scrub`.
- Nothing identifying the user is stored that a feature does not need. The upload confirmation keeps a
  salted hash of the account email, not the email; nothing is sent anywhere except YouTube, the Gemini
  API and NotebookLM. No telemetry.
- The books go only to NotebookLM: they are built in memory, written to a temporary file for each
  upload, and not kept. A local copy is made only with `run --save DIR`. Working files (captions,
  labels, sorting answers, upload records) live under `~/.notebook-alchemy/` and `clean` removes them.
- The books hold the speakers' own words. Gemini labels, sorts and orders passages; it never writes the
  passages, and punctuation it adds is used only when `knowledge.same_words` confirms the words are
  unchanged. A kept chunk the model leaves unlabelled is kept, never dropped.
- Every paid Gemini answer is cached by what the request saw (transcript, prompt version and model;
  passages and taxonomy; section contents and month), so a rerun pays only for what changed. Labels are
  never made by the fallback model (`allow_fallback=False`); a used-up daily quota stops the step and
  the next run resumes.
- Upload content goes to `notebooklm` as a file, never on the command line (Windows limits one argument
  to about 32,000 characters). NotebookLM can reset a source title to its file name after processing, so
  an upload waits, corrects titles, and reads the notebook back before it reports success. An upload
  replaces or removes only sources it recorded as its own (by id); a book that fails to upload keeps its
  old copy, and nothing is uploaded while videos still wait for Gemini.
- Text that comes from outside (titles, errors, paths) is passed through `ui.esc` before Rich prints it.
- Output follows Google's `gcloud` conventions, through the helpers in `ui.py`: everyday words by
  default (topic, book, section, passage; no internal terms), detail such as paths, tokens and models through `ui.detail` (shown with `-v`), names in
  `[brackets]` (`ui.name`), `Warning:` and `Error:` on stderr, a next step as a command to run
  (`ui.next_command`), and yes/no questions as `Do you want to continue (y/N)?`. Say "sign in", not
  "log in", and do not write "please".
- Model names live in `config.py`; prices and model capabilities live in `gemini.py`.

## Validation

Run from the repository root (`python` may be `py` on Windows or `python3` on macOS and Linux).

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest
python -m notebook_alchemy.cli test
```

- Tests are hermetic: `tests/conftest.py` isolates the working directory and home, and blocks real
  processes and sockets. Inject a runner or fake instead of calling out. A test that needs the real
  network or a real NotebookLM account does not belong in this suite.
- A new guard needs a test that fails with the guard removed.
- Do not run the NotebookLM upload or Gemini paths against an account without its owner's consent.

## Process

- Link the GitHub issue a change belongs to, and note in it what changed, the commands run with their
  results, and anything left open.
- Install the pre-push secret scan once per clone: `git config core.hooksPath .githooks`.
- Hosted GitHub Actions runs are not authorized by default. Validate locally; open a pull request
  to `main` or push to `main` (both start `validate.yml`) only after the maintainer approves that
  run, and record the approval as `OPERATIONS.md` (Hosted Execution) says.
- Keep the repository free of personal data: no real emails (use `@example.test`), names, local paths,
  notebook ids or account details in code, tests, docs or recordings. Security reports go through
  GitHub private vulnerability reporting, as `SECURITY.md` says.
