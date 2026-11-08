"""Notebook Alchemy: a YouTube channel's words, as topic books in a NotebookLM notebook."""

__version__ = "0.3.0"

from .config import AlchemyConfig
from .engine import AlchemyEngine

__all__ = ["AlchemyConfig", "AlchemyEngine", "__version__"]
