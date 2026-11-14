# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.2.x   | Yes       |
| < 0.2   | No        |

## How Credentials Are Handled

- The Claude API key is read from the environment or a git-ignored `.env`, sent only in a request
  header by the official `anthropic` SDK, and removed from any error text before it is shown or logged.
  Use a key scoped to a Console workspace of its own, with a monthly spend limit, so a leaked key can
  reach nothing else and cost little.
- The API address is fixed in the code, and `.env` is read only for this tool's own settings
  (`ANTHROPIC_API_KEY` and `ALCHEMY_*`), so a `.env` in the folder a run starts from cannot send the key
  elsewhere or set the `notebooklm` command's own variables. Two of its settings do choose the
  sign-in that receives uploads (`ALCHEMY_SESSION_FILE`, `ALCHEMY_NOTEBOOKLM_PROFILE`); `status` names that
  account, and the first upload to an account asks first.
- The NotebookLM sign-in is kept by the notebooklm-py library under
  `~/.notebooklm/profiles/<profile>/`. This tool opens it through that library, in its own process,
  only to talk to NotebookLM; the library keeps the file's cookies current. This tool's own code never
  reads, copies, logs or exports the file's contents.
- Document content reaches NotebookLM as a temporary file, removed when its upload ends.
- `.gitignore` blocks `.env`, session and token files, and the `vault/` folder older versions wrote. A pre-push hook scans
  pushed commits with gitleaks, and GitHub secret scanning with push protection is enabled.

## Reporting a Vulnerability

Do not open a public issue. Use GitHub private vulnerability reporting:

1. Open the [Security tab](https://github.com/gagexhill/notebook-alchemy/security) of this repository.
2. Choose **Report a vulnerability**.
3. Include the affected command or file, reproduction steps, and the version from
   `notebook-alchemy --version`.

Reports are acknowledged within 48 hours. A fix ships before any public advisory, and reporters are
credited unless they ask otherwise.
