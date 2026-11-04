# Operations

Current operating map for notebook-alchemy, a personally owned open-source CLI (MIT, gagexhill). Company
rules are in `the maintainer's STANDARDS.md`; this file records what is specific to this repository.

## Operating Map

| Surface | Purpose | Source of truth |
|---|---|---|
| Open work | Bugs, features, release tracking | GitHub issues in `gagexhill/notebook-alchemy`; the audit remediation parent is #16 |
| Completed decisions | Material decisions and their rationale | `DEVLOG.md` |
| Agent rules | Invariants, validation commands | `AGENTS.md` |
| Runtime defaults | Model, concurrency, paths | `src/notebook_alchemy/config.py`, `.env.example` |
| Model prices | Cost estimates in `dry-run` | `src/notebook_alchemy/gemini.py` (`PRICING_USD_PER_M`) |
| Dependency updates | Weekly pip and Actions PRs | `.github/dependabot.yml` |

## Validation

Local validation is the gate. Run from the repository root.

```powershell
py -m pip install -e ".[dev]"
py -m ruff check .
py -m ruff format --check .
py -m pytest
py -m notebook_alchemy.cli test
py -m pip_audit
```

`pip_audit` reports known vulnerabilities in installed dependencies; patch on the severity clock in
`STANDARDS.md` (Access And Credentials).

## Hooks

```powershell
git config core.hooksPath .githooks
```

`pre-push` scans the commits being pushed with gitleaks (`winget install Gitleaks.Gitleaks`). Allowlist a
known-public value narrowly in `.gitleaks.toml`. A bypass is `GITLEAKS_BYPASS=1` with
`HOOK_BYPASS_REASON`, and is recorded in the git directory.

## Hosted Execution

| Purpose | Authorization | Reference |
|---|---|---|
| Development validation (`.github/workflows/validate.yml`) | None on record. The workflow triggers on pushes to `main` and on pull requests, so no run is authorized until the Owner records one here | #30 |
| Release | None on record | #33 |

A workflow file, included runner allowance, or an older run is not permission. Verify the repository's
Actions setting before publishing a branch that would trigger a run.

## Credentials

| Credential | Class | Where it lives | Rotation |
|---|---|---|---|
| `GEMINI_API_KEY` | credential | Local `.env` (git-ignored), never committed | 90 days, or at once if it was ever printed or logged |
| NotebookLM session (cookies) | credential | `~/.notebooklm/profiles/<profile>/storage_state.json`, owned by the `notebooklm` CLI | Re-run `notebook-alchemy login` when `login --check` fails |

Neither value goes in an issue, a log, a commit, or a test. Tests use obvious placeholders.

## GitHub Native Settings

Live state belongs to GitHub; this table records what was set and how to read it back.

| Setting | Value | Read back |
|---|---|---|
| Description and topics | Match the README thesis | `gh repo view --json description,repositoryTopics` |
| Wiki | Disabled (unused) | `gh repo view --json hasWikiEnabled` |
| Delete branch on merge | Enabled | `gh repo view --json deleteBranchOnMerge` |
| Ruleset `main-protection` | Blocks deletion and force-push of `main`; no required checks, because hosted checks are not authorized | `gh api repos/gagexhill/notebook-alchemy/rulesets` |
| Private vulnerability reporting | Enabled | `gh api repos/gagexhill/notebook-alchemy/private-vulnerability-reporting` |
| Secret scanning and push protection | Enabled | `gh api repos/gagexhill/notebook-alchemy --jq .security_and_analysis` |
| Labels | Maintainer taxonomy: work type, `area:*`, `severity:*`, `owner-action`, `reproduced` | `gh label list` |

To restore any row, apply the same value with `gh repo edit` or `gh api`; no setting here is mirrored in
the repository.

## Release

Tracked in #33. A release names its source commit, built version, package digest, published artifact and
installed result, with unrun gates left open.
