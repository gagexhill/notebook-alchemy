# Operations

Maintainer runbook: how to validate, scan, release and regenerate assets. User documentation is in
`README.md`; rules for changes are in `AGENTS.md`.

## Where Things Are

| Surface | Purpose | Source of truth |
|---|---|---|
| Open work | Bugs, features, releases | GitHub issues |
| Completed decisions | Design decisions and their rationale | `DEVLOG.md` |
| Rules for changes | Invariants, validation commands | `AGENTS.md` |
| Runtime defaults | Model, concurrency, paths | `src/notebook_alchemy/config.py`, `.env.example` |
| Model prices | Cost estimates in `dry-run` | `src/notebook_alchemy/gemini.py` (`PRICING_USD_PER_M`) |
| Dependency updates | Weekly pip and Actions pull requests | `.github/dependabot.yml` |

## Validation

Local validation is the gate. Run from the repository root (`python` may be `py` on Windows).

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format --check .
python -m pytest
python -m notebook_alchemy.cli test
python -m pip_audit
```

`pip_audit` reports known vulnerabilities in installed dependencies.

## Continuous Integration

`.github/workflows/validate.yml` runs the same checks, a dependency audit and a secret scan on every pull
request to `main` and every push to `main`, on Linux and Windows. It needs no secrets.

## Secret Scan Hook

```bash
git config core.hooksPath .githooks
```

`pre-push` scans the commits being pushed with [gitleaks](https://github.com/gitleaks/gitleaks#installing)
(Homebrew, winget, Scoop or a release binary). Allowlist a known-public value narrowly in `.gitleaks.toml`.
A bypass is `GITLEAKS_BYPASS=1` with `HOOK_BYPASS_REASON`, and is recorded in the git directory.

## Release Assets

Both assets are generated from real output by repeatable scripts, so nothing is drawn by hand.

```bash
python scripts/social_preview.py   # docs/images/social-preview.png (1280x640)
python scripts/render_demo.py docs/images/dry-run.svg "notebook-alchemy dry-run" -- dry-run <channel-url>
```

`render_demo.py` runs the command for real and records the terminal with Rich's SVG export. Record from a
neutral folder with a neutral notebook name, and check the SVG for local paths and account names before
committing it. The social preview is uploaded under Settings, General, Social preview.

## Release

Tracked in a GitHub issue. A release names its source commit, built version, package digest, published
artifact and installed result, with unrun gates left open.
