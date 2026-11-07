"""Local vault layout and atomic output replacement."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import AlchemyConfig
from .errors import VaultError
from .synthesizer import safe_filename

logger = logging.getLogger(__name__)


def source_slug(title: str) -> str:
    """Directory name for one source (a channel, playlist or notebook)."""
    return safe_filename(title).lower()


class VaultManager:
    """Output lives in `<vault>/<source-slug>/compendiums/`, one directory per source."""

    def __init__(self, config: AlchemyConfig) -> None:
        self.config = config
        self.vault_path = config.vault_path

    def compendium_dir(self, title: str) -> Path:
        return self.vault_path / source_slug(title) / "compendiums"

    def existing_compendium_dirs(self) -> list[Path]:
        """Every generated compendium directory (`<vault>/<source>/compendiums`)."""
        if not self.vault_path.is_dir():
            return []
        return [p for p in sorted(self.vault_path.glob("*/compendiums")) if p.is_dir()]

    @staticmethod
    def stage_dir(final: Path) -> Path:
        """A fresh sibling directory to build into."""
        stage = final.with_name(final.name + ".tmp")
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        return stage

    @staticmethod
    def replace_dir(staged: Path, final: Path) -> None:
        """Swap a fully built directory into place. The old one is restored if the swap fails."""
        backup = final.with_name(final.name + ".old")
        if backup.exists():
            shutil.rmtree(backup)
        had_old = final.exists()
        try:
            if had_old:
                final.rename(backup)
            staged.rename(final)
        except OSError as exc:
            if had_old and backup.exists() and not final.exists():
                backup.rename(final)
            raise VaultError(
                f"Could not write the vault directory {final}: {exc}",
                hint="Close programs that have files in it open (editors, sync tools) and retry.",
            ) from exc
        if had_old:
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                logger.warning("Could not remove the previous build %s: %s", backup, exc)
