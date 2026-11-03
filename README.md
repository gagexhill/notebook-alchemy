# Notebook Alchemy

> Orchestration and synthesis engine bridging source documents, audio overviews, and structured knowledge vaults.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## Overview

**Notebook Alchemy** is an open-source orchestration engine designed to streamline multi-source knowledge synthesis. It facilitates ingest, synthesis, and synchronization of research sources, turning complex source material into structured artifacts and persistent markdown vaults.

### Core Capabilities

- **Source Orchestration:** Ingest, structure, and categorize documents, transcripts, and web sources into unified notebook collections.
- **Synthesis Pipeline:** Trigger structured synthesis routines to produce executive briefs, deep dives, and analytical crosswalks.
- **Vault Synchronization:** Mirror notebook artifacts directly into local markdown vaults (such as Obsidian) with preserved frontmatter, bidirectional links, and citation references.
- **Security & Privacy First:** Built from the ground up with strict credential sandboxing. State files, active session tokens, and raw credentials are kept isolated and excluded from version control.

---

## Architecture

```
notebook-alchemy/
├── src/notebook_alchemy/
│   ├── client.py      # Base communication and session interface
│   ├── config.py      # Environment and engine configuration
│   ├── engine.py      # Core AlchemyEngine synthesis and pipeline runner
│   ├── vault.py       # Local markdown vault synchronization manager
│   └── cli.py         # Command-line interface
├── tests/             # Unit and integration test suite
└── pyproject.toml     # Packaging and dependency specification
```

---

## Quickstart

### Prerequisites

- Python 3.10 or higher
- Git

### Installation

Clone the repository and install the package in development mode:

```bash
git clone https://github.com/gagexhill/notebook-alchemy.git
cd notebook-alchemy

# Create and activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Unix/macOS:
# source .venv/bin/activate

# Install package dependencies
pip install -e ".[dev]"
```

### Configuration

Copy `.env.example` to `.env` and adjust the paths to your local setup:

```bash
cp .env.example .env
```

> **Security Warning:** Never commit `.env`, session state files, or cookies to version control. The repository `.gitignore` strictly blocks all credential and session files by default.

---

## Usage

```bash
# Verify installation and view available commands
notebook-alchemy --help

# Inspect configuration and status
notebook-alchemy status

# Sync sources to your local vault
notebook-alchemy sync --vault-path ./vault
```

---

## Development

Run tests with `pytest`:

```bash
pytest
```

---

## License

This project is licensed under the terms of the [MIT License](LICENSE).
