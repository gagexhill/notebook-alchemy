# Contributing

Thank you for helping. This repository tracks all work as GitHub issues; open one (or comment on an
existing one) before a larger change so the scope is agreed.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Set up

Python 3.10 or newer and Git.

```bash
git clone https://github.com/gagexhill/notebook-alchemy.git
cd notebook-alchemy
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
git config core.hooksPath .githooks
```

The hook scans pushed commits for secrets with [gitleaks](https://github.com/gitleaks/gitleaks).

## Check your change

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest
python -m notebook_alchemy.cli test
```

The test suite is hermetic. `tests/conftest.py` runs every test in a temporary working directory and
home, and blocks real processes and network connections. Fake the `notebooklm` CLI and Gemini with the
helpers in the existing tests instead of calling them. A new guard needs a test that fails when the guard
is removed.

## Pull requests

1. Branch from `main` with a prefix such as `fix/`, `feat/` or `docs/`.
2. Write imperative commit messages, for example `fix: report failed uploads with their cause`.
3. Reference the issue (`Refs #12`). Use a closing keyword only when the issue's whole acceptance is met.
4. Never commit keys, cookies, session files or `.env`.
5. Update the README when a command or option changes.

The rules every change must keep, for people and coding agents alike, are in [AGENTS.md](AGENTS.md);
the maintainer runbook is [OPERATIONS.md](OPERATIONS.md).
