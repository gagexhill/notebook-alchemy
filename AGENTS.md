# AGENTS.md

Durable rules for agents working in this repository. Company standards live in
`the maintainer's STANDARDS.md` and are consumed by pointer; this file adds only what is specific here.

## Orientation

- `README.md` is orientation and follows the `readme-structure` skill. `OPERATIONS.md` is the current
  operations map. `DEVLOG.md` records completed material decisions. Open work is a GitHub issue in
  this repository, never a document or an inline comment.
- Source is `src/notebook_alchemy/`: `cli.py` (commands), `ui.py` (output), `engine.py` (orchestration),
  `analyzer.py` (discovery, clustering, cost), `channel.py` (YouTube and transcripts), `synthesizer.py`
  (compendiums), `client.py` (NotebookLM through the `notebooklm` CLI), `gemini.py` (Gemini API),
  `vault.py` (local output), `config.py`, `errors.py`.

## Invariants

- Real input never becomes sample data. Sample data appears only behind an explicit `--mock`, is
  labelled in every output, and is never uploaded.
- A success message is printed only after the work is confirmed: check return codes, parse results, and
  raise a typed error from `errors.py` with a message and a next action. No broad `except` that discards
  an outcome.
- The Gemini key goes in the `x-goog-api-key` header only. Never log, print or store it, and never put
  it in a URL. Every raised message passes through `gemini.scrub`.
- Vault output is written to a staging directory and swapped in whole (`vault.replace_dir`). Output is
  namespaced `vault/<source-slug>/compendiums/`.
- Upload content goes to `notebooklm` as a file, never on the command line (Windows limits one argument
  to about 32,000 characters).
- Text that comes from outside (titles, errors, paths) is passed through `ui.esc` before Rich prints it.
- Model names and prices live in `config.py` and `gemini.py` only.

## Validation

Run from the repository root; these are the owning commands.

```powershell
py -m ruff check .
py -m ruff format --check .
py -m pytest
py -m notebook_alchemy.cli test
```

- Tests are hermetic: `tests/conftest.py` isolates the working directory and home, and blocks real
  processes and sockets. Inject a runner or fake instead of calling out. A test that needs the real
  network or a real NotebookLM account does not belong in this suite.
- A new guard needs a test that fails with the guard removed.
- Do not run the NotebookLM upload or Gemini paths against real accounts without the Owner's say-so.

## Process

- Select or open the owning GitHub issue before editing; reconcile it at each checkpoint with commit,
  commands run and results, and the gates left open.
- Hosted GitHub Actions are not authorized by default. `OPERATIONS.md` records the current authorization;
  validate locally. Install hooks per clone: `git config core.hooksPath .githooks`.
- Keep public surfaces free of internal contacts, token paths and secret names. Security reports go
  through GitHub private vulnerability reporting, as `SECURITY.md` says.
- Use the Owner or Managing Member wording, and the brand register in `the maintainer's STANDARDS.md`
  (Brand Voice) for any outward text.
