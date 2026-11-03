# Security Policy

## Supported Versions

We release patches and security fixes for the active major and minor releases.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

---

## Zero-Leak Architecture Guarantee

**Notebook Alchemy** is engineered with strict local sandbox boundaries:

1. **Local State Isolation:** Authentication session tokens and browser state are saved strictly to user profile paths (such as `~/.notebooklm/profiles/default/storage_state.json`) or local ignored directories.
2. **Explicit Blocklist:** The repository `.gitignore` explicitly blocks `.env`, `*state*.json`, `*cookie*`, `.notebooklm/`, `vault/`, `__pycache__/`, and `.venv/`.
3. **Automated Scanning:** Pull requests and pushes are automatically scanned via Gitleaks and dependency vulnerability audits.

---

## Reporting a Vulnerability

If you discover a security vulnerability or potential credential exposure vector within Notebook Alchemy, **please do not open a public issue.**

Instead, please submit a confidential report using GitHub's Private Vulnerability Reporting:

1. Navigate to the **[Security tab](https://github.com/gagexhill/notebook-alchemy/security)** of this repository.
2. Click **"Report a vulnerability"** to open a draft advisory.
3. Include detailed reproduction steps and affected components.

### Our Commitment

- We will acknowledge receipt of your vulnerability report within 48 hours.
- We will coordinate a remediation timeline and issue a patch before publishing any public advisory.
- You will receive attribution in the security release notes (unless you prefer to remain anonymous).
