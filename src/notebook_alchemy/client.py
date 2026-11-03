"""Client abstraction for NotebookLM and knowledge services."""

from __future__ import annotations

import logging
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
    """Base client interface for notebook source and synthesis operations."""

    def __init__(self, config: AlchemyConfig) -> None:
        self.config = config
        self._authenticated = False

    def is_authenticated(self) -> bool:
        """Check whether valid session state is present."""
        return bool(self.config.session_file and self.config.session_file.exists())

    def list_sources(self) -> list[SourceDocument]:
        """List active sources in the notebook."""
        return []

    def ingest_source(self, document: SourceDocument) -> str:
        """Ingest a source document into the notebook context."""
        logger.info("Ingesting document: %s (type=%s)", document.title, document.source_type)
        return document.source_id
