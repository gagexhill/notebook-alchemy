# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.4.x   | Yes       |
| < 0.4   | No        |

## How Credentials Are Handled

- The Claude API key (`ANTHROPIC_API_KEY`) and optional Gemini API key (`GEMINI_API_KEY`, used only
  with explicit `--watch N`) are read from the environment, a folder-local `.env`, or the global
  `~/.notebook-alchemy/.env`. Keys reach only their official SDKs (`anthropic` -> `https://api.anthropic.com`
  in a request header, `google-genai` -> Gemini API), and are scrubbed from errors and logs before
  reaching the terminal or MCP clients. Use keys with monthly spend limits so a leaked key can reach
  nothing else and cost little.
- `.env` loading reads only this tool's own allowlisted names (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
  and `ALCHEMY_*`), so a `.env` in a working directory cannot redirect keys or set the `notebooklm`
  command's own variables. Two settings choose the sign-in that receives uploads (`ALCHEMY_SESSION_FILE`,
  `ALCHEMY_NOTEBOOKLM_PROFILE`); `status` names that account, and the first upload to an account asks first.
- The local MCP server defaults to `stdio` for desktop clients (Claude Desktop, Antigravity, ChatGPT
  Desktop Codex/Work mode), opening zero network ports. When served over `streamable-http`, binding
  is restricted to loopback (`127.0.0.1`), DNS-rebinding protection is always enabled, and a bearer
  token (`ALCHEMY_MCP_TOKEN` or `--token`, checked with constant-time comparison) is always required.
  MCP tools reject non-YouTube URLs/handles (unless `mock=True`) and scrub `ALCHEMY_MCP_TOKEN` along
  with API keys from errors.
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
