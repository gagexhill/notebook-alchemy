# AGENTS.md

Rules for anyone changing this repository, people and coding agents alike. `CONTRIBUTING.md` covers how
to propose a change; this file covers what a change must keep true.

## Orientation

- `README.md` is the user guide. `OPERATIONS.md` is the maintainer runbook. `DEVLOG.md` records
  completed design decisions and why. Open work is a GitHub issue, never a document or an inline comment.
- Source is `src/notebook_alchemy/`: `cli.py` (commands), `ui.py` (output), `engine.py` (orchestration),
  `analyzer.py` (discovery, clustering, cost), `channel.py` (YouTube and transcripts), `synthesizer.py`
  (compendiums), `client.py` (NotebookLM through the `notebooklm` CLI), `gemini.py` (Gemini API),
  `vault.py` (local output), `config.py` (settings), `errors.py` (typed errors), `mock_data.py` (the
  sample catalog behind `--mock`).

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
- Vault output is written to a staging directory and swapped in whole (`vault.replace_dir`). Output is
  namespaced `vault/<source-slug>/compendiums/`.
- Upload content goes to `notebooklm` as a file, never on the command line (Windows limits one argument
  to about 32,000 characters). NotebookLM can reset a source title to its file name after processing, so
  a sync waits, corrects titles, and reads the notebook back before it reports success.
- Text that comes from outside (titles, errors, paths) is passed through `ui.esc` before Rich prints it.
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
