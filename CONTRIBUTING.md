# Contributing to Notebook Alchemy

First off, thank you for considering contributing to **Notebook Alchemy**! Whether you are proposing a new ingestion adapter, refining our thematic clustering heuristics, or improving documentation, your support makes this engine better for everyone.

---

## Code of Conduct

All contributors and maintainers are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md). Please treat all community members with empathy and respect.

---

## Local Development Workflow

### 1. Prerequisites

- Python 3.10+
- Git

### 2. Setting Up Your Environment

```bash
# Fork and clone the repository
git clone https://github.com/gagexhill/notebook-alchemy.git
cd notebook-alchemy

# Create and activate a local virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On macOS/Linux:
# source .venv/bin/activate

# Install editable package along with dev dependencies
pip install -e ".[dev]"
```

### 3. Running Linting and Tests

We use `ruff` for ultra-fast linting/formatting and standard testing:

```bash
# Format code
ruff format .

# Check linting and style rules
ruff check .

# Run test suite
pytest
# Or using standard library unittest:
python -m unittest discover tests
```

---

## Pull Request Guidelines

1. **Branch Naming**: Use descriptive prefixes like `feat/audio-source`, `fix/rpc-timeout`, `docs/gemini-setup`.
2. **Atomic Commits**: Write clear, imperative commit messages (e.g. `feat: add async RSS ingestion pipeline`).
3. **Strict Credential Hygiene**: Ensure no private API keys, emails, cookies, or session files are ever committed.
4. **Tests**: Add unit tests for any new features or bug fixes.
5. **PR Checklist**: Fill out the [Pull Request Template](.github/PULL_REQUEST_TEMPLATE.md) completely.

---

## Questions & Architectural Discussions

Have an idea or question that doesn't fit into an issue? Head over to our [GitHub Discussions](https://github.com/gagexhill/notebook-alchemy/discussions) to brainstorm with maintainers and other users.
