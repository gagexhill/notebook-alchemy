"""Command Line Interface for Notebook Alchemy."""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import AlchemyConfig
from .engine import AlchemyEngine

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="notebook-alchemy")
def main() -> None:
    """Notebook Alchemy: Knowledge synthesis and vault synchronization engine."""


@main.command()
def status() -> None:
    """Display engine operational status and configuration."""
    config = AlchemyConfig.from_env()
    engine = AlchemyEngine(config)
    info = engine.status()

    table = Table(title="Notebook Alchemy Status")
    table.add_column("Property", style="cyan", no_wrap=True)
    table.add_column("Value", style="magenta")

    for key, value in info.items():
        table.add_row(key, str(value))

    console.print(table)


@main.command()
@click.option(
    "--vault-path",
    type=click.Path(),
    help="Target vault path for synchronization.",
)
def sync(vault_path: str | None) -> None:
    """Synchronize notebook sources and artifacts to the local vault."""
    config = AlchemyConfig.from_env()
    if vault_path:
        config.vault_path = click.format_filename(vault_path)

    engine = AlchemyEngine(config)
    engine.vault.ensure_vault()
    notes = engine.vault.list_notes()

    console.print(
        f"[green]Vault at '{engine.vault.vault_path}' initialized with {len(notes)} existing notes.[/green]"
    )


if __name__ == "__main__":
    main()
