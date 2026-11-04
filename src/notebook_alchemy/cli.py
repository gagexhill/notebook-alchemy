"""Command line interface for Notebook Alchemy."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from textwrap import shorten

import click
from rich.panel import Panel
from rich.table import Table

from . import __version__, ui
from .channel import video_id_from_url
from .client import NotebookLMClient
from .config import AlchemyConfig
from .engine import AlchemyEngine
from .errors import AlchemyError, NotebookLMError, SessionError, VaultError
from .ui import esc, handle_errors

MOCK_NOTE = (
    "This run uses the bundled sample catalog, not a real source. Its output is for trying the tool "
    "and is never uploaded to NotebookLM."
)


class OrderedGroup(click.Group):
    """Lists commands in the order they are defined, which follows a first-run path."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.commands)


def _config() -> AlchemyConfig:
    return AlchemyConfig.from_env()


def _engine() -> AlchemyEngine:
    return AlchemyEngine(_config())


def _target(source: str | None, notebook: str | None, mock: bool) -> str:
    target = notebook or source
    if target:
        return target
    if mock:
        return "sample catalog"
    raise click.UsageError(
        "Give a YouTube channel, playlist or video URL (or an existing notebook), or pass --mock "
        "to try the sample catalog.\n\nExample: notebook-alchemy dry-run https://www.youtube.com/@Hyperplexed"
    )


def _short(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _source_dir(engine: AlchemyEngine, notebook: str | None, source_dir: Path | None) -> Path:
    if source_dir:
        return source_dir
    if not notebook:
        raise click.UsageError("Pass --notebook NAME (the source's name) or --source-dir PATH.")
    candidate = engine.vault.compendium_dir(notebook)
    if candidate.is_dir():
        return candidate
    available = [str(p.parent.name) for p in engine.vault.existing_compendium_dirs()]
    raise VaultError(
        f"No compendiums found for '{notebook}' at {candidate}.",
        hint=(
            "Available sources: " + ", ".join(available) + ". Use --source-dir with one of them."
            if available
            else "Run `notebook-alchemy run <source>` first."
        ),
    )


def _cost_line(report) -> str:
    return (
        f"~{report.total_estimated_tokens:,} transcript tokens, estimated "
        f"${report.total_estimated_cost_usd:.4f} with {esc(report.model)}"
    )


@click.group(
    cls=OrderedGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    epilog=(
        "\b\nFirst run:\n"
        "  notebook-alchemy test                    check the install, no network needed\n"
        "  notebook-alchemy dry-run <youtube-url>   preview videos, domains and cost\n"
        "  notebook-alchemy run <youtube-url>       build compendiums (and upload them)\n"
        "\nDocs: https://github.com/gagexhill/notebook-alchemy"
    ),
)
@click.version_option(version=__version__, prog_name="notebook-alchemy")
@click.option("-q", "--quiet", is_flag=True, help="Only print errors and final results.")
@click.option("-v", "--verbose", count=True, help="Show log output (-v) or debug detail (-vv).")
@click.option("--no-color", is_flag=True, help="Disable colour (the NO_COLOR variable also works).")
@click.option("--debug", is_flag=True, help="Print full tracebacks for unexpected errors.")
def cli(quiet: bool, verbose: int, no_color: bool, debug: bool) -> None:
    """Notebook Alchemy: turn a YouTube channel into NotebookLM-ready domain compendiums.

    It discovers every video, groups them into subject domains, synthesizes one document per
    domain (with Gemini, or as an offline index), and uploads the set to a NotebookLM notebook so a
    large archive fits in a few sources.
    """
    ui.configure(quiet=quiet, no_color=no_color or bool(os.getenv("NO_COLOR")), debug=debug)
    level = logging.DEBUG if verbose > 1 else logging.INFO if verbose else logging.WARNING
    if quiet:
        level = logging.ERROR
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "yt_dlp", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --- run -----------------------------------------------------------------------------------------


@cli.command(
    name="run",
    epilog=(
        "\b\nExamples:\n"
        "  notebook-alchemy run https://www.youtube.com/@Hyperplexed\n"
        "  notebook-alchemy run @Hyperplexed --no-sync          build locally, upload nothing\n"
        '  notebook-alchemy run <url> -N "CSS Course" --yes     name the notebook, skip prompts\n'
        "  notebook-alchemy run --mock --no-sync                try the sample catalog\n"
    ),
)
@click.argument("source", required=False)
@click.option("-n", "--notebook", help="YouTube URL or existing notebook (alternative to SOURCE).")
@click.option("-N", "--notebook-name", help="Name for the destination NotebookLM notebook.")
@click.option("--offline", is_flag=True, help="No Gemini calls: keyword clustering, offline indexes.")
@click.option("--mock", is_flag=True, help="Use the bundled sample catalog instead of a real source.")
@click.option("--limit", type=click.IntRange(min=1), help="Only scan the first N videos.")
@click.option(
    "--max-per-vol",
    type=click.IntRange(min=1),
    default=50,
    show_default=True,
    help="Most videos per compendium volume.",
)
@click.option("--write/--preview-only", default=True, help="Write the vault (default) or preview only.")
@click.option("--sync/--no-sync", default=True, help="Upload to NotebookLM (default) or build locally.")
@click.option("-y", "--yes", is_flag=True, help="Accept defaults without prompting.")
@handle_errors
def run_pipeline(
    source: str | None,
    notebook: str | None,
    notebook_name: str | None,
    offline: bool,
    mock: bool,
    limit: int | None,
    max_per_vol: int,
    write: bool,
    sync: bool,
    yes: bool,
) -> None:
    """Distill a YouTube channel, playlist or video into domain compendiums."""
    target = _target(source, notebook, mock)
    engine = _engine()
    console = ui.console

    if mock and sync:
        sync = False
        ui.warn("Sample data is never uploaded; continuing with --no-sync.")
    do_sync = sync and write

    if do_sync:
        with console.status("Checking your NotebookLM session..."):
            session = engine.client.check_session()
        if not session.ok:
            raise SessionError(
                session.detail,
                hint="Run `notebook-alchemy login`, or add --no-sync to build files without uploading.",
            )
        ui.success(f"NotebookLM session is active ({session.notebooks} notebook(s)).")

    total_steps = 3 if do_sync else 2
    live = engine.config.has_gemini_key and not offline

    ui.step(1, total_steps, "Discover and group videos")
    with ui.Reporter() as reporter:
        report = engine.analyzer_for(offline).generate_dry_run(
            target, use_mock=mock, limit=limit, progress=reporter
        )
    if report.is_mock:
        ui.banner("Sample data", MOCK_NOTE)
    ui.info(
        f"  [bold]{esc(report.notebook_title)}[/bold]: {len(report.youtube_videos)} videos in "
        f"{len(report.domain_videos)} domains ({report.cluster_method} clustering)"
    )
    ui.info(f"  {_cost_line(report)}")
    ui.info(
        "  Mode: "
        + (
            f"Gemini synthesis ({esc(engine.config.gemini_model)})"
            if live
            else "offline index (titles only, no summaries)"
        )
    )
    if not live and not offline:
        ui.warn("GEMINI_API_KEY is not set, so volumes are offline indexes. See `notebook-alchemy status`.")
    for note in report.notes:
        ui.warn(esc(note))

    final_name = notebook_name or report.notebook_title
    if do_sync and not notebook_name and not yes and sys.stdin.isatty():
        console.print()
        final_name = click.prompt("NotebookLM notebook name", default=report.notebook_title).strip() or (
            report.notebook_title
        )

    ui.step(2, total_steps, "Build compendiums")
    with ui.Reporter() as reporter:
        result = engine.run_distillation(
            force_offline=offline,
            max_videos_per_volume=max_per_vol,
            write_to_vault=write,
            dry_run=report,
            progress=reporter,
        )

    table = Table(title="Compendiums", header_style="bold green", title_justify="left")
    table.add_column("Domain", style="cyan", overflow="fold")
    table.add_column("Vol", justify="right")
    table.add_column("Videos", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Mode")
    for c in result.compendiums:
        table.add_row(
            esc(c.domain),
            str(c.volume),
            str(len(c.titles)),
            f"~{c.token_count:,}",
            "gemini" if c.is_live_synthesized else "offline",
        )
    console.print(table)
    for w in result.warnings:
        if w not in report.notes:
            ui.warn(esc(w))

    where = (
        f"{_short(result.vault_directory)}  (index: INDEX.md)"
        if result.vault_directory
        else "not written (--preview-only)"
    )
    console.print(
        Panel(
            f"[bold]{result.total_compendiums}[/bold] compendiums from "
            f"[bold]{result.total_sources_scanned}[/bold] videos, ~{result.total_tokens:,} tokens\n"
            f"Saved to: {esc(where)}",
            title="Build complete",
            border_style="green",
            padding=(0, 2),
        )
    )

    if do_sync:
        ui.step(3, total_steps, f"Upload to NotebookLM: {final_name}")
        if result.vault_directory is None:
            raise AlchemyError("Nothing was written, so there is nothing to upload.")
        with ui.Reporter() as reporter:
            synced = engine.sync_to_notebooklm(final_name, result.vault_directory, progress=reporter)
        action = "Created" if synced.created else "Updated"
        console.print(
            Panel(
                f"{action} notebook [bold]{esc(final_name)}[/bold]\n"
                f"Uploaded {synced.uploaded} files"
                + (f", replaced {synced.replaced} existing sources" if synced.replaced else "")
                + "\n\nOpen https://notebooklm.google.com to use Audio Overview, Study Guides and Quizzes.",
                title="Upload complete",
                border_style="green",
                padding=(0, 2),
            )
        )
    elif write and not report.is_mock:
        ui.info(
            "\n[dim]Upload later with:[/dim] notebook-alchemy sync -n " + esc(f'"{report.notebook_title}"')
        )


# --- dry-run -------------------------------------------------------------------------------------


@cli.command(
    name="dry-run",
    epilog=(
        "\b\nExamples:\n"
        "  notebook-alchemy dry-run https://www.youtube.com/@Hyperplexed\n"
        "  notebook-alchemy dry-run @Hyperplexed --limit 50 --offline\n"
        "  notebook-alchemy dry-run --mock\n"
    ),
)
@click.argument("source", required=False)
@click.option("-n", "--notebook", help="YouTube URL or existing notebook (alternative to SOURCE).")
@click.option("--limit", type=click.IntRange(min=1), help="Only scan the first N videos.")
@click.option("--offline", is_flag=True, help="No Gemini calls: keyword clustering only.")
@click.option("--mock", is_flag=True, help="Use the bundled sample catalog instead of a real source.")
@handle_errors
def dry_run(source: str | None, notebook: str | None, limit: int | None, offline: bool, mock: bool) -> None:
    """Preview videos, domains and cost. Writes and uploads nothing."""
    target = _target(source, notebook, mock)
    engine = _engine()
    console = ui.console
    ui.info(f"[bold cyan]Analyzing[/bold cyan] {esc(target)}")
    with ui.Reporter() as reporter:
        report = engine.analyzer_for(offline).generate_dry_run(
            target, use_mock=mock, limit=limit, progress=reporter
        )
    if report.is_mock:
        ui.banner("Sample data", MOCK_NOTE)

    origin = {"youtube": "YouTube", "notebooklm": "NotebookLM notebook", "mock": "sample catalog"}[
        report.origin
    ]
    total_videos = max(len(report.youtube_videos), 1)
    summary = (
        f"[bold]Source:[/bold] {esc(report.notebook_title)}"
        + ("" if report.is_mock else f" [dim]({origin})[/dim]")
        + "\n"
        f"[bold]Videos:[/bold] {len(report.youtube_videos)}"
        + (f"   [bold]Other sources:[/bold] {len(report.other_sources)}" if report.other_sources else "")
        + f"\n[bold]Domains:[/bold] {len(report.domain_videos)} ({report.cluster_method} clustering)\n\n"
        f"[bold]Transcript tokens:[/bold] ~{report.total_estimated_tokens:,}\n"
        f"[bold]Input cost:[/bold] ${report.estimated_input_cost_usd:.4f}   "
        f"[bold]Output cost:[/bold] ${report.estimated_synthesis_output_cost_usd:.4f}\n"
        f"[bold green]Estimated total:[/bold green] ${report.total_estimated_cost_usd:.4f} "
        f"[dim]({esc(report.model)}; an estimate from video length)[/dim]"
    )
    console.print(Panel(summary, title="Dry run", border_style="cyan", padding=(0, 2)))

    table = Table(title="Domains", header_style="bold green", title_justify="left")
    table.add_column("Domain", style="cyan", overflow="fold")
    table.add_column("Videos", justify="right")
    table.add_column("Share")
    table.add_column("Examples", style="dim", overflow="fold")
    for domain, vids in sorted(report.domain_videos.items(), key=lambda kv: -len(kv[1])):
        share = len(vids) / total_videos
        samples = "; ".join(esc(shorten(v.title, 50, placeholder="…")) for v in vids[:2])
        table.add_row(esc(domain), str(len(vids)), f"{'█' * max(1, round(share * 12))} {share:.0%}", samples)
    console.print(table)
    for note in report.notes:
        ui.warn(esc(note))
    if not report.is_mock:
        ui.info(f"\n[dim]Next:[/dim] notebook-alchemy run {esc(target)}")


# --- status / login ------------------------------------------------------------------------------


@cli.command(name="status")
@handle_errors
def status() -> None:
    """Check this install: NotebookLM CLI, session file, Gemini key, vault and cache."""
    engine = _engine()
    facts = engine.status()
    has_cli = not str(facts["notebooklm_cli"]).startswith("not found")
    has_session = not str(facts["session_file"]).startswith("none")
    has_key = facts["gemini_api_key"] == "set"

    table = Table(title="Notebook Alchemy", header_style="bold magenta", title_justify="left")
    table.add_column("", width=1)
    table.add_column("Check", style="bold")
    table.add_column("Value", overflow="fold")
    rows = [
        (has_cli, "notebooklm CLI", facts["notebooklm_cli"]),
        (has_session, "NotebookLM session file", facts["session_file"]),
        (has_key, "Gemini API key", facts["gemini_api_key"]),
        (None, "Gemini model", facts["gemini_model"]),
        (None, "Vault", facts["vault_path"]),
        (None, "Transcript cache", facts["cache_dir"]),
        (None, "Parallel fetches", facts["max_concurrency"]),
        (None, "Environment", facts["environment"]),
    ]
    for ok, name, value in rows:
        glyph = "[green]✓[/green]" if ok else "[red]✗[/red]" if ok is False else "[dim]-[/dim]"
        table.add_row(glyph, name, esc(value))
    ui.console.print(table)

    next_steps = []
    if not has_cli:
        next_steps.append("pip install notebooklm-py")
    if not has_session:
        next_steps.append("notebook-alchemy login")
    if not has_key:
        next_steps.append(
            "set GEMINI_API_KEY in .env for synthesized compendiums (offline indexes work without it)"
        )
    if next_steps:
        ui.console.print("\n[bold]Next:[/bold]\n" + "\n".join(f"  - {esc(s)}" for s in next_steps))
    else:
        ui.console.print("\n[green]Ready.[/green] A session file exists. `login --check` confirms it works.")


@cli.command(
    name="login",
    epilog=(
        "\b\nExamples:\n"
        "  notebook-alchemy login                        sign in through a browser window\n"
        "  notebook-alchemy login --browser-cookies edge reuse an Edge sign-in, no window\n"
        "  notebook-alchemy login --check                is the stored session still valid?\n"
    ),
)
@click.option(
    "--browser",
    default="chromium",
    show_default=True,
    type=click.Choice(["chromium", "chrome", "msedge"]),
    help="Browser to open for interactive sign-in.",
)
@click.option(
    "--browser-cookies",
    type=click.Choice(["chrome", "edge", "firefox"]),
    help="Reuse an installed browser's sign-in instead of opening a window.",
)
@click.option("--check", is_flag=True, help="Verify the stored session without signing in.")
@click.option(
    "--timeout",
    type=click.IntRange(min=30),
    default=300,
    show_default=True,
    help="Seconds to wait for you to finish signing in.",
)
@handle_errors
def login(browser: str, browser_cookies: str | None, check: bool, timeout: int) -> None:
    """Sign in to Google NotebookLM (uses the `notebooklm` CLI)."""
    client = NotebookLMClient(_config())
    if check:
        with ui.console.status("Checking your NotebookLM session..."):
            result = client.check_session()
        if not result.ok:
            raise SessionError(result.detail, hint="Run `notebook-alchemy login`.")
        ui.success(f"Session is active ({result.notebooks} notebook(s) visible).")
        return

    if browser_cookies:
        cmd = ["notebooklm", "login", "--browser-cookies", browser_cookies]
        ui.info(f"Importing your {browser_cookies} sign-in...")
    else:
        cmd = ["notebooklm", "login", "--browser", browser, "--browser-timeout", str(timeout)]
        ui.info(f"A {browser} window will open. Sign in to Google; this closes by itself when done.")
    try:
        proc = subprocess.run(
            cmd, check=False, capture_output=bool(browser_cookies), text=True, encoding="utf-8"
        )
    except FileNotFoundError as exc:
        raise NotebookLMError(
            "The `notebooklm` command was not found.",
            hint="Install it with: pip install notebooklm-py",
        ) from exc
    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout or "") if browser_cookies else "").strip()
        raise NotebookLMError(
            f"Sign-in did not complete (exit code {proc.returncode})."
            + (f" {' '.join(detail.split())[:300]}" if detail else ""),
            hint=(
                "Try `notebook-alchemy login --browser-cookies edge`, or set NOTEBOOKLM_AUTH_JSON "
                "with exported session cookies."
            ),
        )
    with ui.console.status("Confirming the session works..."):
        result = client.check_session()
    if result.ok:
        ui.success("Signed in to NotebookLM.")
    else:
        ui.warn(f"Sign-in finished but the session check failed: {esc(result.detail)}")


# --- sync / append -------------------------------------------------------------------------------


@cli.command(
    name="sync",
    epilog=(
        "\b\nExamples:\n"
        "  notebook-alchemy sync -n Hyperplexed\n"
        '  notebook-alchemy sync -n "CSS Course" --source-dir vault/hyperplexed/compendiums\n'
    ),
)
@click.option(
    "-n",
    "--notebook",
    help="NotebookLM notebook to create or update (also selects the vault folder).",
)
@click.option(
    "--source-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Folder of compendiums to upload (default: the vault folder named after --notebook).",
)
@handle_errors
def sync_to_notebooklm(notebook: str | None, source_dir: Path | None) -> None:
    """Upload built compendiums to a notebook, replacing same-titled sources."""
    engine = _engine()
    directory = _source_dir(engine, notebook, source_dir)
    name = notebook or directory.parent.name
    ui.info(f"Uploading {esc(_short(directory))} to notebook [bold]{esc(name)}[/bold]")
    with ui.Reporter() as reporter:
        result = engine.sync_to_notebooklm(name, directory, progress=reporter)
    ui.success(
        f"{'Created' if result.created else 'Updated'} notebook '{esc(name)}': "
        f"uploaded {result.uploaded}, replaced {result.replaced}."
    )


@cli.command(
    name="append",
    epilog=(
        "\b\nExample:\n"
        '  notebook-alchemy append -n Hyperplexed --title "New video" '
        "--url https://youtu.be/dQw4w9WgXcQ\n"
    ),
)
@click.option("-n", "--notebook", help="Source name; selects the vault folder.")
@click.option("--source-dir", type=click.Path(file_okay=False, path_type=Path), help="Compendium folder.")
@click.option("--title", required=True, help="Title of the new video.")
@click.option("--url", required=True, help="URL of the new video.")
@click.option("--topic", help="Domain to file it under (default: clustered automatically).")
@handle_errors
def append_video(
    notebook: str | None, source_dir: Path | None, title: str, url: str, topic: str | None
) -> None:
    """Add one new video to the catalog and its domain compendium without a full rebuild."""
    engine = _engine()
    directory = _source_dir(engine, notebook, source_dir)
    video = {"id": video_id_from_url(url), "title": title, "url": url}
    result = engine.append_incremental_sources(
        [video], directory, domain=topic, notebook_title=notebook or directory.parent.name
    )
    if result.added:
        ui.success(f"Added to the catalog and updated: {esc(', '.join(result.updated_files))}")
        ui.info("[dim]Run `notebook-alchemy sync` to upload the change.[/dim]")
    else:
        ui.warn("That video is already in the catalog; nothing changed.")


# --- clean / test --------------------------------------------------------------------------------


def _is_cache_record(path: Path) -> bool:
    """True for a transcript cache file this tool wrote (JSON object with a video_id)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "video_id" in data


def _remove(path: Path) -> int:
    """Delete a file or directory tree; return the bytes it held. Raises OSError on failure."""
    size = (
        sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        if path.is_dir()
        else path.stat().st_size
    )
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return size


@cli.command(
    name="clean",
    epilog=(
        "\b\nExamples:\n"
        "  notebook-alchemy clean                  clear the transcript cache\n"
        "  notebook-alchemy clean --vaults         delete built compendiums (asks first)\n"
        "  notebook-alchemy clean --all --yes      both, without asking\n"
    ),
)
@click.option("--all", "clean_all", is_flag=True, help="Clear the cache and delete built vaults.")
@click.option("--cache", "clean_cache", is_flag=True, help="Clear the transcript cache.")
@click.option("--vaults", "clean_vaults", is_flag=True, help="Delete built compendium folders.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask before deleting.")
@handle_errors
def clean_pipeline(clean_all: bool, clean_cache: bool, clean_vaults: bool, yes: bool) -> None:
    """Reclaim disk space from the transcript cache and built vaults."""
    config = _config()
    engine = AlchemyEngine(config)
    do_vaults = clean_all or clean_vaults
    do_cache = clean_all or clean_cache or not do_vaults

    targets: list[tuple[str, Path]] = []
    skipped: list[str] = []
    custom_cache = config.cache_dir != AlchemyConfig().cache_dir
    if do_cache and config.cache_dir.is_dir():
        # Only this tool's own cache records, directly in the folder: never other files, never subfolders.
        for f in sorted(config.cache_dir.glob("*.json")):
            if _is_cache_record(f):
                targets.append(("cache", f))
            else:
                skipped.append(f.name)
    if do_vaults:
        for d in engine.vault.existing_compendium_dirs():
            if (d / "INDEX.md").is_file() or (d / "MASTER_SOURCES_CATALOG.md").is_file():
                targets.append(("vault", d))
            else:
                skipped.append(str(d))
    for item in skipped:
        ui.warn(f"Skipped {esc(item)}: it does not look like Notebook Alchemy output.")
    if not targets:
        ui.success("Nothing to clean.")
        return

    cache_targets = [p for kind, p in targets if kind == "cache"]
    vault_targets = [p for kind, p in targets if kind == "vault"]
    ask = (vault_targets or (cache_targets and custom_cache)) and not yes
    if ask:
        if cache_targets and custom_cache:
            ui.console.print(
                f"[bold]{len(cache_targets)} cache files in a custom cache folder "
                f"({esc(config.cache_dir)}) will be deleted.[/bold]"
            )
        if vault_targets:
            ui.console.print("[bold]These built vault folders will be deleted:[/bold]")
            for p in vault_targets:
                ui.console.print(f"  {esc(p)}")
        if not click.confirm("Continue?", default=False):
            ui.warn("Cancelled. Nothing was deleted.")
            return

    freed, failed = 0, []
    for _kind, path in targets:
        try:
            freed += _remove(path)
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    for p in vault_targets:  # drop the now-empty per-source parent
        with contextlib.suppress(OSError):  # not empty or already gone; it was only a container
            p.parent.rmdir()

    if failed:
        raise AlchemyError(
            f"{len(failed)} items could not be deleted: " + "; ".join(failed[:3]),
            hint="Close programs using those files and run the command again.",
        )
    ui.success(f"Removed {len(targets)} items, freed {ui.human_bytes(freed)}.")


@cli.command(name="test")
@handle_errors
def run_automated_test() -> None:
    """Self-test the whole pipeline on sample data. No network, no credentials, nothing kept."""
    started = time.monotonic()
    cwd_vault = Path.cwd() / "vault"
    existed_before = cwd_vault.exists()
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    try:
        config = AlchemyConfig.from_env()
        record("Configuration loads", True, f"environment: {config.env}")
    except AlchemyError as exc:
        record("Configuration loads", False, exc.message)

    with tempfile.TemporaryDirectory(prefix="notebook-alchemy-test-") as tmp:
        tmp_path = Path(tmp)
        test_config = AlchemyConfig(
            env="test",
            vault_path=tmp_path / "vault",
            state_dir=tmp_path / "state",
            cache_dir=tmp_path / "cache",
            gemini_api_key="",
        )
        engine = AlchemyEngine(test_config)
        report = None
        try:
            report = engine.analyzer_for(True).generate_dry_run("sample", use_mock=True)
            ok = len(report.youtube_videos) > 0
            record("Sample catalog loads", ok, f"{len(report.youtube_videos)} sample videos")
            domains = len(report.domain_videos)
            record("Videos group into domains", domains >= 3, f"{domains} domains (keyword method)")
        except AlchemyError as exc:
            record("Sample catalog loads", False, exc.message)

        built = None
        if report:
            try:
                built = engine.run_distillation(force_offline=True, dry_run=report, write_to_vault=True)
                ok = built.total_compendiums >= 3 and bool(built.index_path and built.index_path.exists())
                record("Compendiums build", ok, f"{built.total_compendiums} compendiums plus INDEX.md")
            except AlchemyError as exc:
                record("Compendiums build", False, exc.message)

        if built and built.vault_directory:
            files = [
                f
                for f in built.vault_directory.glob("*.md")
                if f.name not in ("INDEX.md", "MASTER_SOURCES_CATALOG.md")
            ]
            valid = sum(
                1
                for f in files
                if (t := f.read_text(encoding="utf-8")).startswith("---")
                and "domain:" in t
                and "## 2. Video Index" in t
            )
            catalog = (built.vault_directory / "MASTER_SOURCES_CATALOG.md").read_text(encoding="utf-8")
            rows = sum(1 for line in catalog.splitlines() if line.startswith("| ") and "`" in line)
            ok = (
                bool(files)
                and valid == len(files)
                and report is not None
                and rows == len(report.youtube_videos)
            )
            record(
                "Vault files are valid",
                ok,
                f"{valid}/{len(files)} compendiums, {rows} catalog rows",
            )

    left_behind = cwd_vault.exists() and not existed_before
    record("Nothing written outside the temp folder", not left_behind, "working directory untouched")

    table = Table(title="Self-test", header_style="bold", title_justify="left")
    table.add_column("", width=1)
    table.add_column("Check", style="bold")
    table.add_column("Result", style="dim", overflow="fold")
    for name, ok, detail in results:
        table.add_row("[green]✓[/green]" if ok else "[red]✗[/red]", name, esc(detail))
    ui.console.print(table)

    elapsed = time.monotonic() - started
    if all(ok for _n, ok, _d in results):
        ui.success(f"All {len(results)} checks passed in {elapsed:.1f}s.")
    else:
        failed = sum(1 for _n, ok, _d in results if not ok)
        ui.console.print(
            f"\n[bold red]{failed} of {len(results)} checks failed.[/bold red] Run with --debug for detail."
        )
        sys.exit(1)


def main() -> None:
    """Entry point for the notebook-alchemy command."""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            # a redirected stream (for example under a test) may not support reconfigure
            with contextlib.suppress(AttributeError, OSError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    cli()


if __name__ == "__main__":
    main()
