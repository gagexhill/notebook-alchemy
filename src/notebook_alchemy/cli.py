"""Command Line Interface for Notebook Alchemy."""

from __future__ import annotations

import subprocess
import sys
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .analyzer import NotebookAnalyzer
from .config import AlchemyConfig
from .engine import AlchemyEngine

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="notebook-alchemy")
def main() -> None:
    """Notebook Alchemy: Knowledge synthesis and vault synchronization engine."""


@main.command()
@click.option(
    "--browser",
    type=click.Choice(["chrome", "chromium", "msedge"]),
    default="chrome",
    help="Browser to launch for Google authentication.",
)
def login(browser: str) -> None:
    """Authenticate with your Google account via browser."""
    console.print(f"[bold cyan]Launching {browser} for Google authentication...[/bold cyan]")
    cmd = ["notebooklm", "login", "--browser", browser]
    try:
        subprocess.run(cmd, check=True)
        console.print("[bold green]✓ Successfully authenticated with Google NotebookLM![/bold green]")
    except subprocess.CalledProcessError as err:
        console.print(f"[bold red]Authentication failed with code {err.returncode}[/bold red]")
        sys.exit(err.returncode)


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


@main.command(name="dry-run")
@click.option(
    "--notebook",
    "-n",
    default="Portable Entrepreneur",
    help="Title or ID of the notebook to analyze.",
)
def dry_run(notebook: str) -> None:
    """Run a pre-flight inspection and token/cost analysis without modifying NotebookLM."""
    console.print(f"[bold cyan]Running dry-run analysis for notebook: '{notebook}'...[/bold cyan]")
    analyzer = NotebookAnalyzer()

    try:
        report = analyzer.generate_dry_run(notebook_query=notebook)
    except Exception as exc:
        console.print(f"[bold red]Dry run failed:[/bold red] {exc}")
        sys.exit(1)

    # Summary Panel
    summary_text = (
        f"[bold]Notebook:[/bold] {report.notebook_title} ([dim]{report.notebook_id}[/dim])\n"
        f"[bold]Total Sources:[/bold] {report.total_sources}\n"
        f"[bold]YouTube Video Sources:[/bold] {len(report.youtube_videos)}\n"
        f"[bold]Other Documents/Notes:[/bold] {len(report.other_sources)}\n\n"
        f"[bold]Total Estimated Tokens:[/bold] ~{report.total_estimated_tokens:,}\n"
        f"[bold]Estimated Gemini Flash Input Cost:[/bold] ${report.estimated_input_cost_usd:.4f} USD\n"
        f"[bold]Estimated Synthesis Output Cost:[/bold] ${report.estimated_synthesis_output_cost_usd:.4f} USD\n"
        f"[bold green]Total Estimated Cost:[/bold green] ${report.total_estimated_cost_usd:.4f} USD"
    )
    console.print(Panel(summary_text, title="Dry-Run Cost & Token Sizing", border_style="cyan"))

    # Topic Clusters Table
    topic_table = Table(title="Human-Readable Domain Topic Clusters (Preview)")
    topic_table.add_column("Domain Topic", style="green", no_wrap=True)
    topic_table.add_column("Video Count", style="yellow", justify="right")
    topic_table.add_column("Sample Titles", style="dim")

    for topic, titles in report.domain_topic_clusters.items():
        sample = " • " + " • ".join(titles[:2])
        if len(titles) > 2:
            sample += f" ... (+{len(titles) - 2} more)"
        topic_table.add_row(topic, str(len(titles)), sample)

    console.print(topic_table)


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
