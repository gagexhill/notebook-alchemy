"""Client abstraction for NotebookLM and knowledge services."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from .config import AlchemyConfig

logger = logging.getLogger(__name__)


@dataclass
class SourceDocument:
    """Represents a source document ingested into Notebook Alchemy."""

    source_id: str
    title: str
    content: str
    source_type: str = "document"  # document, url, transcript, note
    metadata: dict[str, Any] | None = None


class BaseNotebookClient:
    """Client interface for NotebookLM notebook management and source ingestion."""

    def __init__(self, config: AlchemyConfig, profile: str = "default") -> None:
        self.config = config
        self.profile = profile

    def is_authenticated(self) -> bool:
        """Check whether valid session state is present."""
        if self.config.session_file and self.config.session_file.is_file():
            return True
        default_storage = self.config.state_dir / "storage_state.json"
        return default_storage.is_file()

    def list_notebooks(self) -> list[dict[str, Any]]:
        """List all notebooks in the account."""
        if not self.is_authenticated():
            return []
        cmd = ["notebooklm", "--profile", self.profile, "list", "--json"]
        try:
            res = subprocess.run(
                cmd, check=False, capture_output=True, text=True, encoding="utf-8", timeout=3.0
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                return data if isinstance(data, list) else data.get("notebooks", [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to list notebooks from live CLI: %s", exc)
        return []

    def find_notebook(self, query: str) -> dict[str, Any] | None:
        """Find notebook by title substring or exact ID."""
        notebooks = self.list_notebooks()
        q_lower = query.lower().strip()
        for nb in notebooks:
            title = nb.get("title", "")
            nb_id = nb.get("id", "")
            if q_lower in title.lower() or q_lower == nb_id.lower():
                return nb
        return None

    def create_notebook(self, title: str) -> str:
        """Create a new notebook in NotebookLM if it does not already exist."""
        if not self.is_authenticated():
            return f"nb_{title.lower().replace(' ', '_')}"

        existing = self.find_notebook(title)
        if existing:
            logger.info("Found existing notebook: %s (%s)", title, existing.get("id"))
            return str(existing.get("id"))

        cmd = ["notebooklm", "--profile", self.profile, "create", title, "--json"]
        try:
            res = subprocess.run(
                cmd, check=False, capture_output=True, text=True, encoding="utf-8", timeout=5.0
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                nb_id = data.get("id") or data.get("notebook_id") or "nb_created"
                logger.info("Created new NotebookLM notebook: %s (id=%s)", title, nb_id)
                return str(nb_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create notebook via live CLI (%s); using generated ID.", exc)

        return f"nb_{title.lower().replace(' ', '_')}"

    def add_source_document(self, notebook_id: str, document: SourceDocument) -> str:
        """Upload a synthesized domain compendium into the targeted NotebookLM notebook."""
        logger.info(
            "Uploading domain compendium '%s' to notebook %s (type=%s)",
            document.title,
            notebook_id,
            document.source_type,
        )

        if not self.is_authenticated():
            return document.source_id

        cmd = [
            "notebooklm",
            "--profile",
            self.profile,
            "source",
            "add",
            "-n",
            notebook_id,
            "--title",
            document.title,
            "--text",
            document.content,
            "--json",
        ]
        try:
            res = subprocess.run(
                cmd, check=False, capture_output=True, text=True, encoding="utf-8", timeout=5.0
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                return str(data.get("id") or document.source_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Live source upload skipped or offline: %s", exc)

        return document.source_id

    def list_sources(self) -> list[SourceDocument]:
        """List active sources in the notebook."""
        return []

    def ingest_source(self, document: SourceDocument) -> str:
        """Ingest a source document into local context."""
        logger.info("Ingesting document: %s (type=%s)", document.title, document.source_type)
        return document.source_id
