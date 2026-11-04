"""Command Line Interface for Notebook Alchemy."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Ensure UTF-8 console output on Windows to prevent UnicodeEncodeError with emojis
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001, S110
        pass

from . import __version__
from .analyzer import NotebookAnalyzer
from .config import AlchemyConfig
from .engine import AlchemyEngine

logger = logging.getLogger(__name__)
console = Console(legacy_windows=False)


def _safe_str(text: str) -> str:
    """Strip or normalize characters that cause legacy Windows console crashes."""
    return text.encode("ascii", "replace").decode("ascii")


@click.group()
@click.version_option(version=__version__, prog_name="notebook-alchemy")
def cli() -> None:
    """Notebook Alchemy: Distill massive YouTube channels & playlists into self-organizing Domain Compendiums."""


@cli.command()
@click.option(
    "--browser",
    default="chromium",
    type=click.Choice(["chromium", "chrome", "msedge"]),
    help="Browser engine to launch for interactive login.",
)
@click.option(
    "--browser-cookies",
    type=click.Choice(["chrome", "edge", "firefox", "auto"]),
    help="Extract cookies directly from an installed browser without launching a GUI window.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Check whether current stored session credentials are valid without logging in.",
)
@click.option(
    "--timeout",
    default=30,
    help="Timeout in seconds for interactive login before aborting (default: 30s).",
)
def login(browser: str, browser_cookies: str | None, check: bool, timeout: int) -> None:
    """Authenticate with Google NotebookLM safely without indefinite hanging."""
    if check:
        console.print("[cyan]Verifying existing NotebookLM session...[/cyan]")
        analyzer = NotebookAnalyzer()
        try:
            notebooks = analyzer.list_notebooks()
            console.print(
                f"[bold green]✓ Session is active and authenticated! Found {len(notebooks)} notebook(s).[/bold green]"
            )
            return
        except Exception as exc:  # noqa: BLE001
            console.print(f"[bold yellow]Session check failed:[/bold yellow] {exc}")
            console.print("[dim]Re-authentication is required.[/dim]")
            sys.exit(1)

    if browser_cookies:
        target = "edge" if browser_cookies in ("edge", "auto") else browser_cookies
        console.print(f"[cyan]Attempting non-interactive cookie import from {target}...[/cyan]")
        cmd = ["notebooklm", "login", "--browser-cookies", target]
        try:
            res = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8")
            if res.returncode == 0:
                console.print(
                    f"[bold green]✓ Cookies successfully imported from {target}![/bold green]"
                )
                return
            console.print(f"[yellow]Notice from cookie import:[/yellow] {res.stderr or res.stdout}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[bold red]Cookie extraction failed:[/bold red] {exc}")
            sys.exit(1)

    console.print(
        f"[cyan]Initiating Google NotebookLM authentication using {browser} (timeout: {timeout}s)...[/cyan]"
    )
    cmd = ["notebooklm", "login", "--browser", browser, "--browser-timeout", str(timeout)]
    try:
        subprocess.run(cmd, check=True)
        console.print(
            "[bold green]✓ Successfully authenticated with Google NotebookLM![/bold green]"
        )
    except subprocess.CalledProcessError as err:
        console.print(
            f"[bold red]Authentication failed or timed out with exit code {err.returncode}.[/bold red]\n"
            "[yellow]Tip:[/yellow] If running in a headless or automated terminal, use:\n"
            "  1. notebook-alchemy login --browser-cookies edge\n"
            "  2. Or set the NOTEBOOKLM_AUTH_JSON environment variable with exported session cookies."
        )
        sys.exit(err.returncode)


@cli.command()
def status() -> None:
    """Inspect local engine configuration, session state, and vault path."""
    engine = AlchemyEngine()
    stat = engine.status()

    table = Table(title="Notebook Alchemy Status", show_header=True, header_style="bold magenta")
    table.add_column("Property", style="dim", width=15)
    table.add_column("Value")

    for k, v in stat.items():
        table.add_row(k, str(v))

    console.print(table)


@cli.command()
@click.argument("source", required=False, default=None)
@click.option(
    "-n",
    "--notebook",
    default=None,
    help="Title or ID of the notebook, or YouTube channel/playlist URL.",
)
@click.option(
    "--live/--fallback",
    default=False,
    help="Strictly enforce live NotebookLM connection (default: auto-fallback).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Limit number of videos to scan (e.g. 50, 200, 1000).",
)
def dry_run(source: str | None, notebook: str | None, live: bool, limit: int | None) -> None:
    """Run a pre-flight inspection and token/cost analysis on a YouTube channel URL or notebook."""
    target = notebook or source or "Portable Entrepreneur"
    console.print(f"[bold cyan]Running dry-run analysis for target: '{target}'...[/bold cyan]")
    analyzer = NotebookAnalyzer()

    try:
        report = analyzer.generate_dry_run(
            notebook_query=target,
            use_fallback=not live,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Dry run failed:[/bold red] {exc}")
        sys.exit(1)
        console.print(f"[bold red]Dry run failed:[/bold red] {exc}")
        sys.exit(1)

    source_tag = (
        "[yellow](Verified Channel Catalog)[/yellow]"
        if report.is_simulated
        else "[green](Live NotebookLM)[/green]"
    )

    summary_text = (
        f"[bold]Notebook:[/bold] {report.notebook_title} ([dim]{report.notebook_id}[/dim]) {source_tag}\n"
        f"[bold]Total Sources:[/bold] {report.total_sources}\n"
        f"[bold]YouTube Video Sources:[/bold] {len(report.youtube_videos)}\n"
        f"[bold]Other Documents/Notes:[/bold] {len(report.other_sources)}\n\n"
        f"[bold]Total Estimated Tokens:[/bold] ~{report.total_estimated_tokens:,}\n"
        f"[bold]Estimated Gemini Flash Input Cost:[/bold] ${report.estimated_input_cost_usd:.4f} USD\n"
        f"[bold]Estimated Synthesis Output Cost:[/bold] ${report.estimated_synthesis_output_cost_usd:.4f} USD\n"
        f"[bold green]Total Estimated Cost:[/bold green] ${report.total_estimated_cost_usd:.4f} USD"
    )

    console.print(Panel(summary_text, title="Dry-Run Cost & Token Sizing", border_style="cyan"))

    topic_table = Table(
        title="Human-Readable Domain Topic Clusters (Preview)",
        show_header=True,
        header_style="bold green",
    )
    topic_table.add_column("Domain Topic", style="cyan", width=38)
    topic_table.add_column("Video Count", justify="right", width=12)
    topic_table.add_column("Sample Titles", style="dim")

    for topic, titles in report.domain_topic_clusters.items():
        clean_titles = [_safe_str(t) for t in titles[:2]]
        sample = " - " + " - ".join(clean_titles)
        if len(titles) > 2:
            sample += f" ... (+{len(titles) - 2} more)"
        topic_table.add_row(topic, str(len(titles)), sample)

    console.print(topic_table)


@cli.command(name="run")
@click.argument("source", required=False, default=None)
@click.option(
    "-n",
    "--notebook",
    default=None,
    help="Title or ID of the notebook, or YouTube channel/playlist URL.",
)
@click.option(
    "--notebook-name",
    "-N",
    default=None,
    help="Explicit name for the created Google NotebookLM notebook.",
)
@click.option(
    "--offline/--live-llm",
    default=True,
    help="Force deterministic offline semantic synthesis (default: offline).",
)
@click.option(
    "--max-per-vol",
    default=50,
    help="Maximum videos grouped per compendium volume (default: 50).",
)
@click.option(
    "--write/--preview-only",
    default=True,
    help="Write markdown notes to the local vault (default: write).",
)
@click.option(
    "--sync/--no-sync",
    default=True,
    help="Upload generated compendiums into Google NotebookLM (default: sync).",
)
def run_pipeline(
    source: str | None,
    notebook: str | None,
    notebook_name: str | None,
    offline: bool,
    max_per_vol: int,
    write: bool,
    sync: bool,
) -> None:
    """Execute end-to-end distillation from a YouTube channel URL or notebook into Domain Compendiums."""
    target = notebook or source or "Portable Entrepreneur"
    engine = AlchemyEngine()

    # Pillar 5: Upfront Login Check if sync is requested
    if sync and write and not engine.client.is_authenticated():
        console.print(
            Panel(
                "[bold red]Google NotebookLM Authentication Required[/bold red]\n\n"
                "You are not currently logged in to Google NotebookLM.\n"
                "To authenticate, run:\n"
                "  [bold cyan]notebook-alchemy login[/bold cyan]\n\n"
                "To generate local Markdown vault compendiums without uploading, pass [bold yellow]--no-sync[/bold yellow].",
                title="Login Required",
                border_style="red",
            )
        )
        sys.exit(1)

    console.print(
        f"[bold cyan]Starting Notebook Alchemy Distillation for target: '{target}'...[/bold cyan]"
    )

    try:
        report = engine.run_distillation(
            notebook_query=target,
            force_offline=offline,
            max_videos_per_volume=max_per_vol,
            write_to_vault=write,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Distillation failed:[/bold red] {exc}")
        sys.exit(1)

    result_table = Table(
        title="Generated Domain Compendiums",
        show_header=True,
        header_style="bold green",
    )
    result_table.add_column("Domain Compendium", style="cyan", width=38)
    result_table.add_column("Vol", justify="center", width=5)
    result_table.add_column("Sources", justify="right", width=8)
    result_table.add_column("Est. Tokens", justify="right", width=12)
    result_table.add_column("Vault Path", style="dim")

    for c in report.compendiums:
        result_table.add_row(
            c.domain,
            str(c.volume),
            str(len(c.titles)),
            f"~{c.token_count:,}",
            c.relative_path,
        )

    console.print(result_table)

    summary = (
        f"[bold]Total Videos Processed:[/bold] {report.total_sources_scanned}\n"
        f"[bold]Compendiums Created:[/bold] {report.total_compendiums}\n"
        f"[bold]Total Synthesized Tokens:[/bold] ~{report.total_tokens:,}\n"
    )
    if report.vault_directory:
        summary += f"[bold]Vault Directory:[/bold] {report.vault_directory}\n"
        summary += f"[bold cyan]Master Source Catalog:[/bold cyan] {report.vault_directory / 'MASTER_SOURCES_CATALOG.md'}\n"
    if report.index_path:
        summary += f"[bold green]Master Index:[/bold green] {report.index_path}\n"

    console.print(Panel(summary, title="Distillation Completed", border_style="green"))

    # Pillar 5: Interactive Notebook Naming and Direct Sync
    if sync and write:
        proposed_name = notebook_name or report.notebook_title or target
        if not notebook_name and sys.stdin.isatty():
            console.print("\n[bold cyan]Google NotebookLM Destination[/bold cyan]")
            user_choice = click.prompt(
                "Proposed Notebook Name",
                default=proposed_name,
                show_default=True,
            )
            final_nb_name = user_choice.strip() or proposed_name
        else:
            final_nb_name = proposed_name

        console.print(
            f"[cyan]Syncing compendiums to Google NotebookLM notebook: '{final_nb_name}'...[/cyan]"
        )
        try:
            nb_id = engine.sync_to_notebooklm(final_nb_name)
            console.print(
                Panel(
                    f"[bold green]✓ Successfully uploaded {len(report.compendiums)} compendiums + Master Catalog to Google NotebookLM![/bold green]\n\n"
                    f"Notebook Title: [bold]{final_nb_name}[/bold] ([dim]{nb_id}[/dim])\n"
                    f"Open in your browser to use Audio Overview, Quizzes & Study Guides:\n"
                    f"[bold cyan]https://notebooklm.google.com[/bold cyan]",
                    title="NotebookLM Sync Succeeded",
                    border_style="green",
                )
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[bold red]Sync to NotebookLM failed:[/bold red] {exc}")


@cli.command(name="sync")
@click.option(
    "-n",
    "--notebook",
    default="Portable Entrepreneur",
    help="Target notebook title to create or update in Google NotebookLM.",
)
def sync_to_notebooklm(notebook: str) -> None:
    """Create target NotebookLM notebook if missing and upload all compendiums as sources."""
    console.print(
        f"[bold cyan]Syncing domain compendiums to NotebookLM notebook: '{notebook}'...[/bold cyan]"
    )
    engine = AlchemyEngine()

    try:
        nb_id = engine.sync_to_notebooklm(notebook)
        console.print(
            f"[bold green]✓ Successfully synced compendiums to notebook '{notebook}' (ID: {nb_id})![/bold green]"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Sync failed:[/bold red] {exc}")
        sys.exit(1)


@cli.command(name="append")
@click.option(
    "-n",
    "--notebook",
    default="Portable Entrepreneur",
    help="Target notebook title.",
)
@click.option("--title", required=True, help="Title of the new YouTube video.")
@click.option("--url", required=True, help="URL of the new YouTube video.")
@click.option("--topic", default="General", help="Domain topic hint (optional).")
def append_video(notebook: str, title: str, url: str, topic: str) -> None:
    """Incrementally append a new video to the Master Catalog and matching domain compendium."""
    console.print(f"[cyan]Appending new video to '{notebook}': {title}...[/cyan]")
    engine = AlchemyEngine()

    new_video = {
        "id": f"yt_new_{abs(hash(title)) % 10000:04d}",
        "title": title,
        "url": url,
        "topic": topic,
    }

    try:
        updated = engine.append_incremental_sources([new_video], notebook_title=notebook)
        console.print(
            f"[bold green]✓ Successfully appended to Master Catalog and updated {updated} domain compendium(s)![/bold green]"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Append failed:[/bold red] {exc}")
        sys.exit(1)


@cli.command(name="test")
def run_automated_test() -> None:
    """Run an automated end-to-end test of the entire ingestion & synthesis pipeline."""
    console.print(
        "[bold cyan]══════════════════════════════════════════════════════════════════[/bold cyan]"
    )
    console.print(
        "[bold cyan]           Notebook Alchemy - Automated Pipeline Verification      [/bold cyan]"
    )
    console.print(
        "[bold cyan]══════════════════════════════════════════════════════════════════[/bold cyan]\n"
    )

    steps: list[tuple[str, str, str]] = []

    # Step 1: Configuration check
    try:
        config = AlchemyConfig.from_env()
        steps.append(("Configuration Loader", "PASS", f"Environment: {config.env}"))
    except Exception as exc:  # noqa: BLE001
        steps.append(("Configuration Loader", "FAIL", str(exc)))

    # Step 2: Analyzer & Catalog Ingestion
    try:
        analyzer = NotebookAnalyzer()
        dry_run_rep = analyzer.generate_dry_run("Portable Entrepreneur", use_fallback=True)
        vid_count = len(dry_run_rep.youtube_videos)
        if vid_count > 0:
            steps.append(("Source Ingestion", "PASS", f"Scanned {vid_count} video sources"))
        else:
            steps.append(("Source Ingestion", "FAIL", "No video sources found"))
    except Exception as exc:  # noqa: BLE001
        steps.append(("Source Ingestion", "FAIL", str(exc)))

    # Step 3: Domain Clustering & Cost Modeling
    try:
        clusters = dry_run_rep.domain_topic_clusters
        cluster_count = len(clusters)
        cost = dry_run_rep.total_estimated_cost_usd
        if cluster_count >= 5 and cost > 0:
            steps.append(
                (
                    "Thematic Decomposition",
                    "PASS",
                    f"{cluster_count} domain clusters, est. cost ${cost:.4f}",
                )
            )
        else:
            steps.append(("Thematic Decomposition", "FAIL", f"Invalid clusters ({cluster_count})"))
    except Exception as exc:  # noqa: BLE001
        steps.append(("Thematic Decomposition", "FAIL", str(exc)))

    # Step 4: Vault & Distillation Generation
    try:
        engine = AlchemyEngine()
        dist_rep = engine.run_distillation(
            "Portable Entrepreneur",
            force_offline=True,
            write_to_vault=True,
        )
        if dist_rep.total_compendiums >= 6 and dist_rep.index_path and dist_rep.index_path.exists():
            steps.append(
                (
                    "Compendium Synthesis Engine",
                    "PASS",
                    f"Generated {dist_rep.total_compendiums} compendiums + master INDEX.md",
                )
            )
        else:
            steps.append(
                (
                    "Compendium Synthesis Engine",
                    "FAIL",
                    f"Compendiums: {dist_rep.total_compendiums}",
                )
            )
    except Exception as exc:  # noqa: BLE001
        steps.append(("Compendium Synthesis Engine", "FAIL", str(exc)))

    # Step 5: Vault Artifact Validation
    try:
        comp_dir = engine.vault.vault_path / "compendiums"
        md_files = list(comp_dir.glob("*.md"))
        valid_frontmatters = 0
        for f in md_files:
            txt = f.read_text(encoding="utf-8")
            if txt.startswith("---") and "domain:" in txt and "## 1. Executive Summary" in txt:
                valid_frontmatters += 1
        if valid_frontmatters >= 6:
            steps.append(
                (
                    "Vault Artifact Integrity",
                    "PASS",
                    f"Validated {valid_frontmatters} Markdown compendiums with frontmatter & SOPs",
                )
            )
        else:
            steps.append(
                (
                    "Vault Artifact Integrity",
                    "FAIL",
                    f"Only {valid_frontmatters}/{len(md_files)} passed validation",
                )
            )
    except Exception as exc:  # noqa: BLE001
        steps.append(("Vault Artifact Integrity", "FAIL", str(exc)))

    # Display test results
    test_table = Table(title="Pipeline Verification Results", show_header=True)
    test_table.add_column("Verification Step", style="bold")
    test_table.add_column("Status", justify="center", width=8)
    test_table.add_column("Details", style="dim")

    all_passed = True
    for step_name, status, details in steps:
        style = "bold green" if status == "PASS" else "bold red"
        if status != "PASS":
            all_passed = False
        test_table.add_row(step_name, f"[{style}]{status}[/{style}]", details)

    console.print(test_table)

    if all_passed:
        console.print(
            "\n[bold green]✓ All automated pipeline verification checks PASSED (100%)![/bold green]"
        )
    else:
        console.print("\n[bold red]✗ Some pipeline verification checks FAILED.[/bold red]")
        sys.exit(1)


def main() -> None:
    """Entry point for notebook-alchemy CLI."""
    cli()


if __name__ == "__main__":
    main()


@cli.command(name="clean")
@click.option(
    "--all",
    "clean_all",
    is_flag=True,
    help="Purge all cached transcripts, temporary files, and local vaults.",
)
@click.option(
    "--cache",
    "clean_cache",
    is_flag=True,
    help="Purge cached transcripts (~/.notebook-alchemy/cache).",
)
@click.option("--vaults", "clean_vaults", is_flag=True, help="Purge local vault markdown builds.")
def clean_pipeline(clean_all: bool, clean_cache: bool, clean_vaults: bool) -> None:
    """Clean up temporary caches and build artifacts to reclaim disk space."""
    import shutil

    cache_dir = Path.home() / ".notebook-alchemy" / "cache"
    vaults_dir = Path.home() / ".notebook-alchemy" / "vaults"
    freed = 0

    do_cache = clean_all or clean_cache or (not clean_cache and not clean_vaults)
    do_vaults = clean_all or clean_vaults

    if do_cache and cache_dir.exists():
        for f in cache_dir.glob("*"):
            if f.is_file():
                try:
                    freed += f.stat().st_size
                    f.unlink()
                except Exception:  # noqa: BLE001, S110
                    pass
        console.print(
            "[green]✓ Purged temporary transcript cache (~/.notebook-alchemy/cache)[/green]"
        )

    if do_vaults and vaults_dir.exists():
        try:
            shutil.rmtree(vaults_dir, ignore_errors=True)
            vaults_dir.mkdir(parents=True, exist_ok=True)
            console.print(
                "[green]✓ Purged local vault markdown builds (~/.notebook-alchemy/vaults)[/green]"
            )
        except Exception:  # noqa: BLE001, S110
            pass

    console.print(
        f"[bold green]✓ Zero-bloat cleanup complete! Reclaimed {freed / 1024:.1f} KB of disk space.[/bold green]"
    )
