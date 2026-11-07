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
from .client import (
    AccountIdentity,
    NotebookLMClient,
    consent_file,
    record_upload_consent,
    upload_confirmed,
)
from .config import (
    DEFAULT_MAX_PER_VOLUME,
    LOG_LEVELS,
    AlchemyConfig,
    is_usable_api_key,
    update_dotenv,
    validate_profile_name,
)
from .engine import AlchemyEngine
from .errors import AlchemyError, ConfigError, NotebookLMError, SessionError, VaultError
from .synthesizer import (
    CATALOG_FILENAME,
    INDEX_FILENAME,
)
from .ui import esc, handle_errors

MOCK_NOTE = (
    "This run uses the bundled sample catalog, not a real source. Its output is for trying the tool "
    "and is never uploaded to NotebookLM."
)


class OrderedGroup(click.Group):
    """Lists commands in the order they are defined, which follows a first-run path."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.commands)


COOKIE_BROWSERS = ("chrome", "edge", "firefox", "brave")
SETUP_KEYS = ("GEMINI_API_KEY", "ALCHEMY_NOTEBOOKLM_PROFILE", "ALCHEMY_VAULT_PATH")
KEY_BILLING_NOTE = (
    "The key bills to the Google Cloud project it was created in (see aistudio.google.com/apikey). "
    "A Google AI subscription plan does not change Gemini API quota or billing."
)


def _config(profile: str | None = None) -> AlchemyConfig:
    config = AlchemyConfig.from_env()
    if profile:
        config.notebooklm_profile = validate_profile_name(profile, source="--profile")
    return config


def _engine(profile: str | None = None) -> AlchemyEngine:
    return AlchemyEngine(_config(profile))


def _interactive() -> bool:
    return sys.stdin.isatty()


profile_option = click.option(
    "-p",
    "--profile",
    help="notebooklm profile (Google account) to use. Default: ALCHEMY_NOTEBOOKLM_PROFILE or 'default'.",
)


def _cookie_browser(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    """`chrome`, `edge`, `firefox` or `brave`, optionally `chrome::<browser profile>`."""
    if value is None:
        return None
    base, sep, rest = value.partition("::")
    base = base.strip().lower()
    if base not in COOKIE_BROWSERS:
        raise click.BadParameter(f"use one of {', '.join(COOKIE_BROWSERS)}, optionally as chrome::<profile>.")
    return f"{base}{sep}{rest}"  # the browser lower-cased, its profile name as typed


def _account_text(identity: AccountIdentity) -> str:
    """Markup-safe description of the upload account. Unknown is said plainly, never guessed."""
    where = f"session file {identity.storage}" if identity.storage else f"profile {identity.profile}"
    if identity.email:
        return f"{esc(identity.email)} [dim]({esc(where)})[/dim]"
    reason = f"; {identity.detail}" if identity.detail else ""
    return f"unknown [dim]({esc(where)}{esc(reason)})[/dim]"


def _show_upload_target(engine: AlchemyEngine) -> AccountIdentity:
    identity = engine.client.upload_account()
    ui.info(f"  Upload account: {_account_text(identity)}")
    if not identity.email:
        ui.info(
            "  [dim]Record it with `notebook-alchemy login --account <email> --browser-cookies chrome`"
            " or `notebook-alchemy setup`.[/dim]"
        )
    ui.info(f"  Gemini API key: {'set' if engine.config.has_gemini_key else 'not set'}")
    return identity


def _confirm_first_upload(identity: AccountIdentity, yes: bool) -> None:
    """Ask before the first upload to a profile, and again if its account changes. --yes skips.

    An unknown account is asked about every time: two different unknown accounts look the same.
    """
    if yes or upload_confirmed(identity):
        return
    target = f"profile '{identity.profile}' ({identity.display} Google account)"
    first = "an upload" if identity.email is None else "the first upload"
    if not _interactive():
        raise SessionError(
            f"This is {first} to NotebookLM {target}, and it needs your confirmation.",
            hint=(
                "Run the command in a terminal to confirm, or pass --yes. Record the account with "
                "`notebook-alchemy login --browser-cookies chrome --account <email>` so it is asked "
                "only once."
            ),
        )
    ui.console.print(
        f"\n[bold]Upload to NotebookLM {esc(target)}.[/bold] Notebooks will be created in that account."
    )
    if not click.confirm("Upload to this account?", default=False):
        raise AlchemyError(
            "Cancelled. Nothing was uploaded.",
            hint="Choose another account with --profile, or run `notebook-alchemy setup`.",
        )
    record_upload_consent(identity)


def _same_account_as_confirmed(engine: AlchemyEngine, confirmed: AccountIdentity) -> None:
    """Read the account again before uploading: the profile may have been re-signed during the build."""
    now = engine.client.upload_account()
    if (now.email or "").casefold() != (confirmed.email or "").casefold():
        raise SessionError(
            f"The upload account changed during the build: it was {confirmed.display}, "
            f"now it is {now.display}. Nothing was uploaded.",
            hint="The files are built; check the account with `notebook-alchemy status`, then run "
            "`notebook-alchemy sync` to upload them.",
        )


def _gemini_usage_text(engine: AlchemyEngine) -> str:
    """What Gemini actually used, as Google reported it, priced at today's listed rates."""
    rows = engine.gemini.usage() if engine.gemini else []
    if not rows:
        return ""
    lines = [
        f"{esc(model)}: {tokens_in:,} input and {tokens_out:,} output tokens (about ${cost:.4f})"
        for model, tokens_in, tokens_out, cost in rows
    ]
    return "\nGemini used " + "; ".join(lines) + " [dim](output includes thinking)[/dim]"


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
        "  notebook-alchemy setup                   Gemini key, NotebookLM account, vault\n"
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
    # -vv / -v / -q win; otherwise ALCHEMY_LOG_LEVEL (validated in AlchemyConfig) or warnings only.
    env_level = os.getenv("ALCHEMY_LOG_LEVEL", "").strip().upper()
    level = LOG_LEVELS.get(env_level, logging.WARNING)
    if verbose:
        level = logging.DEBUG if verbose > 1 else logging.INFO
    if quiet:
        level = logging.ERROR
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger().setLevel(level)  # basicConfig is a no-op when handlers already exist
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
    default=DEFAULT_MAX_PER_VOLUME,
    show_default=True,
    help="Most videos per compendium volume.",
)
@click.option("--write/--preview-only", default=True, help="Write the vault (default) or preview only.")
@click.option("--sync/--no-sync", default=True, help="Upload to NotebookLM (default) or build locally.")
@click.option("-y", "--yes", is_flag=True, help="Accept defaults without prompting.")
@profile_option
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
    profile: str | None,
) -> None:
    """Distill a YouTube channel, playlist or video into domain compendiums."""
    target = _target(source, notebook, mock)
    engine = _engine(profile)
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
        confirmed = _show_upload_target(engine)
        _confirm_first_upload(confirmed, yes)

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
        f"{_short(result.vault_directory)}  (index: {INDEX_FILENAME})"
        if result.vault_directory
        else "not written (--preview-only)"
    )
    console.print(
        Panel(
            f"[bold]{result.total_compendiums}[/bold] compendiums from "
            f"[bold]{result.total_sources_scanned}[/bold] videos, ~{result.total_tokens:,} tokens\n"
            f"Saved to: {esc(where)}" + _gemini_usage_text(engine),
            title="Build complete",
            border_style="green",
            padding=(0, 2),
        )
    )

    if do_sync:
        ui.step(3, total_steps, f"Upload to NotebookLM: {final_name}")
        if result.vault_directory is None:
            raise AlchemyError("Nothing was written, so there is nothing to upload.")
        _same_account_as_confirmed(engine, confirmed)
        with ui.Reporter() as reporter:
            synced = engine.sync_to_notebooklm(final_name, result.vault_directory, progress=reporter)
        action = "Created" if synced.created else "Updated"
        console.print(
            Panel(
                f"{action} notebook [bold]{esc(final_name)}[/bold]\n"
                f"Uploaded {synced.uploaded} files"
                + (f", replaced {synced.replaced} existing sources" if synced.replaced else "")
                + (f", removed {synced.pruned} stale sources from the earlier run" if synced.pruned else "")
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
        f"[bold]Gemini requests:[/bold] ~{report.estimated_gemini_requests} "
        f"[dim](the free tier allows a limited number per day)[/dim]\n"
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
@profile_option
@handle_errors
def status(profile: str | None) -> None:
    """Check this install: NotebookLM CLI, upload account, Gemini key, vault and cache."""
    engine = _engine(profile)
    facts = engine.status()
    identity = engine.client.upload_account()
    has_cli = not str(facts["notebooklm_cli"]).startswith("not found")
    has_session = not str(facts["session_file"]).startswith("none")
    has_key = facts["gemini_api_key"] == "set"

    table = Table(title="Notebook Alchemy", header_style="bold magenta", title_justify="left")
    table.add_column("", width=1)
    table.add_column("Check", style="bold")
    table.add_column("Value", overflow="fold")
    rows = [
        (has_cli, "notebooklm CLI", esc(facts["notebooklm_cli"])),
        (None, "NotebookLM profile", esc(facts["notebooklm_profile"])),
        (has_session, "NotebookLM session file", esc(facts["session_file"])),
        (bool(identity.email) if has_session else None, "Upload account", _account_text(identity)),
        (has_key, "Gemini API key", esc(facts["gemini_api_key"])),
        (None, "Gemini model", esc(facts["gemini_model"])),
        (None, "Vault", esc(facts["vault_path"])),
        (None, "Transcript cache", esc(facts["cache_dir"])),
        (None, "Parallel fetches", esc(facts["max_concurrency"])),
        (None, "Environment", esc(facts["environment"])),
    ]
    for ok, name, value in rows:
        glyph = "[green]✓[/green]" if ok else "[red]✗[/red]" if ok is False else "[dim]-[/dim]"
        table.add_row(glyph, name, value)
    ui.console.print(table)
    if has_key:
        ui.console.print(f"[dim]{esc(KEY_BILLING_NOTE)}[/dim]")

    next_steps = []
    if not has_cli:
        next_steps.append("pip install notebooklm-py")
    if not has_session:
        next_steps.append("notebook-alchemy setup (or: notebook-alchemy login)")
    elif not identity.email:
        next_steps.append(
            "record the upload account: notebook-alchemy login --browser-cookies chrome --account <email>"
        )
    if not has_key:
        next_steps.append(
            "set GEMINI_API_KEY with `notebook-alchemy setup` for synthesized compendiums "
            "(offline indexes work without it)"
        )
    if next_steps:
        ui.console.print("\n[bold]Next:[/bold]\n" + "\n".join(f"  - {esc(s)}" for s in next_steps))
    else:
        ui.console.print("\n[green]Ready.[/green] A session file exists. `login --check` confirms it works.")


def _sign_in(
    client: NotebookLMClient,
    *,
    browser: str = "chromium",
    browser_cookies: str | None = None,
    account: str | None = None,
    timeout: int = 300,
    fresh: bool = False,
) -> AccountIdentity:
    """Run `notebooklm login` for this profile, then read back which account it recorded."""
    if account and browser_cookies:
        # Check before signing in: a login with the wrong account would overwrite the profile.
        try:
            available = client.browser_accounts(browser_cookies)
        except NotebookLMError as exc:
            available = None
            ui.warn(f"Could not list the browser's accounts first ({esc(exc.message)}); trying the sign-in.")
        if available is not None and account.strip().casefold() not in {a.casefold() for a in available}:
            raise SessionError(
                f"{account} is not signed in to {browser_cookies}. Signed in: "
                + (", ".join(available) or "no Google accounts")
                + ". Nothing was changed.",
                hint=(
                    f"Sign in to that account in {browser_cookies.split('::', 1)[0]} first, "
                    "or pick a listed one."
                ),
            )
    cmd = client.login_command()
    if browser_cookies:
        cmd += ["--browser-cookies", browser_cookies]
        if account:
            cmd += ["--account", account]
        override = client.storage_override()
        into = f"session file {override}" if override else f"profile '{client.profile}'"
        ui.info(f"Importing your {esc(browser_cookies)} sign-in into {esc(into)}...")
    else:
        cmd += ["--browser", browser, "--browser-timeout", str(timeout)]
        if fresh:
            cmd.append("--fresh")
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
        if browser_cookies:
            browser_name = browser_cookies.split("::", 1)[0]
            hint = (
                f"List the accounts that browser holds with `notebooklm auth inspect --browser "
                f"{browser_name}`, sign in to Google in it first, or try another browser "
                f"({', '.join(COOKIE_BROWSERS)})."
            )
        else:
            hint = (
                "If no window opened, install its browser once with `python -m playwright install "
                "chromium`, or use `--browser chrome`. Reusing a browser you are already signed in "
                "to also works: `notebook-alchemy login --browser-cookies chrome`."
            )
        raise NotebookLMError(
            f"Sign-in did not complete (exit code {proc.returncode})."
            + (f" {' '.join(detail.split())[:300]}" if detail else ""),
            hint=hint,
        )
    identity = client.upload_account()
    if account and (identity.email or "").casefold() != account.strip().casefold():
        raise SessionError(
            f"Sign-in finished, but profile '{client.profile}' now records the account "
            f"{identity.display}, not {account}. Uploads through this profile would go to that account.",
            hint=(
                "Check the email with `notebooklm auth inspect --browser "
                f"{(browser_cookies or 'chrome').split('::', 1)[0]}` and sign in again before uploading."
            ),
        )
    with ui.console.status("Confirming the session works..."):
        result = client.check_session()
    if not result.ok:
        raise SessionError(
            f"Sign-in finished but the session check failed: {result.detail}",
            hint="Run `notebook-alchemy login` again.",
        )
    ui.success(f"Signed in to NotebookLM. Upload account: {_account_text(identity)}.")
    return identity


@cli.command(
    name="login",
    epilog=(
        "\b\nExamples:\n"
        "  notebook-alchemy login                        sign in through a browser window\n"
        "  notebook-alchemy login --browser-cookies chrome  reuse a Chrome sign-in, no window\n"
        "  notebook-alchemy login --browser-cookies chrome --account you@example.com --profile personal\n"
        "                                                pick one of several signed-in accounts\n"
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
    callback=_cookie_browser,
    help="Reuse an installed browser's sign-in (chrome, edge, firefox, brave; or chrome::<profile>).",
)
@click.option(
    "--account",
    help=(
        "Google account email. With --browser-cookies it picks that account; with a sign-in window "
        "it is checked after you sign in."
    ),
)
@click.option("--fresh", is_flag=True, help="Start the sign-in window with a clean browser session.")
@click.option("--check", is_flag=True, help="Verify the stored session without signing in.")
@click.option(
    "--timeout",
    type=click.IntRange(min=30),
    default=300,
    show_default=True,
    help="Seconds to wait for you to finish signing in.",
)
@profile_option
@handle_errors
def login(
    browser: str,
    browser_cookies: str | None,
    account: str | None,
    fresh: bool,
    check: bool,
    timeout: int,
    profile: str | None,
) -> None:
    """Sign in to Google NotebookLM for one profile (uses the `notebooklm` CLI)."""
    client = NotebookLMClient(_config(profile))
    if check:
        with ui.console.status("Checking your NotebookLM session..."):
            result = client.check_session()
        if not result.ok:
            raise SessionError(result.detail, hint="Run `notebook-alchemy login`.")
        identity = client.upload_account()
        ui.success(f"Session is active ({result.notebooks} notebook(s) visible).")
        ui.info(f"  Upload account: {_account_text(identity)}")
        return
    _sign_in(
        client,
        browser=browser,
        browser_cookies=browser_cookies,
        account=account,
        timeout=timeout,
        fresh=fresh,
    )


@cli.command(
    name="setup",
    epilog=(
        "\b\nRe-run it any time to change the key, the account or the vault.\n"
        "Example:\n"
        "  notebook-alchemy setup\n"
    ),
)
@handle_errors
def setup() -> None:
    """Guided setup: Gemini API key, NotebookLM account and vault folder, saved to .env."""
    if not _interactive():
        raise click.UsageError("setup asks questions, so run it in a terminal.")
    env_file = Path(".env")  # the file AlchemyConfig.from_env reads
    # Read before .env is loaded: a value already in the process environment wins over .env.
    from_process = {name for name in SETUP_KEYS if os.getenv(name)}
    try:
        config = _config()
    except ConfigError as exc:
        if "ALCHEMY_NOTEBOOKLM_PROFILE" not in exc.message:
            raise
        ui.warn(f"{esc(exc.message)} Setup will ask for a new one.")
        os.environ["ALCHEMY_NOTEBOOKLM_PROFILE"] = "default"  # this process only
        config = _config()
    saved: list[str] = []

    def save(name: str, value: str) -> None:
        update_dotenv({name: value}, env_file)
        saved.append(name)
        if name in from_process:
            ui.warn(
                f"{name} is also set in your environment, and that value wins over .env. "
                "Remove it from the environment to use what you entered here."
            )

    ui.step(1, 3, "Gemini API key")
    ui.info(f"  Currently: {'set' if config.has_gemini_key else 'not set'} (the key is never shown)")
    ui.info(f"  [dim]{esc(KEY_BILLING_NOTE)}[/dim]")
    ui.info("  [dim]Create a key at https://aistudio.google.com/apikey[/dim]")
    while True:
        entered = click.prompt(
            "  Paste a new key, or press Enter to keep the current one",
            default="",
            show_default=False,
            hide_input=True,
        ).strip()
        if not entered or is_usable_api_key(entered):
            break
        ui.warn("That does not look like an API key. Try again, or press Enter to skip.")
    if entered:
        save("GEMINI_API_KEY", entered)
        ui.success(f"Saved the key to {esc(_short(env_file))}.")
    elif not config.has_gemini_key:
        ui.warn("No key: compendiums will be offline indexes (titles only).")

    ui.step(2, 3, "NotebookLM account")
    ui.info(
        "  [dim]Uploads go to the Google account of a notebooklm profile. It is independent of the "
        "account that owns the Gemini key.[/dim]"
    )
    while True:
        try:
            profile = validate_profile_name(
                click.prompt("  Profile name", default=config.notebooklm_profile), source="profile"
            )
            break
        except ConfigError as exc:
            ui.warn(f"{esc(exc.message)} {esc(exc.hint or '')}")
    config.notebooklm_profile = profile
    client = NotebookLMClient(config)
    identity = client.upload_account()
    ui.info(f"  Signed in as: {_account_text(identity)}")
    choice = click.prompt(
        "  Sign in: reuse a browser's sign-in (cookies), open a sign-in window (window), or keep",
        type=click.Choice(["cookies", "window", "keep"]),
        default="keep" if identity.email else "cookies",
    )
    if choice == "cookies":
        browser_cookies = click.prompt(
            "  Browser", type=click.Choice(list(COOKIE_BROWSERS)), default="chrome"
        )
        try:
            emails = client.browser_accounts(browser_cookies)
        except NotebookLMError as exc:
            emails = []
            ui.warn(f"Could not list that browser's accounts: {esc(exc.message)}")
        for email in emails:
            ui.info(f"    - {esc(email)}")
        account = click.prompt(
            "  Google account email for uploads", default=emails[0] if len(emails) == 1 else None
        ).strip()
        identity = _sign_in(client, browser_cookies=browser_cookies, account=account)
    elif choice == "window":
        _sign_in(client, fresh=click.confirm("  Start with a clean browser session?", default=True))
        identity = client.upload_account()
    save("ALCHEMY_NOTEBOOKLM_PROFILE", profile)

    ui.step(3, 3, "Vault folder")
    # The default is what .env already says (or "vault"), never the resolved path with the home folder.
    vault = click.prompt(
        "  Where should compendiums be written?",
        default=os.getenv("ALCHEMY_VAULT_PATH", "").strip() or "vault",
    )
    vault_path = Path(vault).expanduser().resolve()
    # Saved as typed (relative or ~/...), so .env does not carry the user's home folder name.
    save("ALCHEMY_VAULT_PATH", vault.strip())

    ui.console.print(
        Panel(
            f"Gemini API key: {'set' if entered or config.has_gemini_key else 'not set'}\n"
            f"Upload account: {_account_text(identity)}\n"
            f"Vault: {esc(vault_path)}\n\n"
            f"Saved {esc(', '.join(saved))} to {esc(_short(env_file))}.",
            title="Setup complete",
            border_style="green",
            padding=(0, 2),
        )
    )


# --- sync / append -------------------------------------------------------------------------------


@cli.command(
    name="sync",
    epilog=(
        "\b\nExamples:\n"
        "  notebook-alchemy sync -n Hyperplexed\n"
        "  notebook-alchemy sync -n Hyperplexed --profile personal --yes\n"
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
@click.option("-y", "--yes", is_flag=True, help="Do not ask before the first upload to an account.")
@profile_option
@handle_errors
def sync_to_notebooklm(notebook: str | None, source_dir: Path | None, yes: bool, profile: str | None) -> None:
    """Upload built compendiums to a notebook, replacing same-titled sources."""
    engine = _engine(profile)
    directory = _source_dir(engine, notebook, source_dir)
    name = notebook or directory.parent.name
    with ui.console.status("Checking your NotebookLM session..."):
        session = engine.client.check_session()
    if not session.ok:
        raise SessionError(session.detail, hint="Run `notebook-alchemy login`, then sync again.")
    _confirm_first_upload(_show_upload_target(engine), yes)
    ui.info(f"Uploading {esc(_short(directory))} to notebook [bold]{esc(name)}[/bold]")
    with ui.Reporter() as reporter:
        result = engine.sync_to_notebooklm(name, directory, progress=reporter)
    ui.success(
        f"{'Created' if result.created else 'Updated'} notebook '{esc(name)}': "
        f"uploaded {result.uploaded}, replaced {result.replaced}"
        + (f", removed {result.pruned} stale." if result.pruned else ".")
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


# Files that mark a folder as this tool's vault output, in current and earlier layouts.
_VAULT_MARKERS = (INDEX_FILENAME, CATALOG_FILENAME)


def _is_sync_record(path: Path) -> bool:
    """A record this tool wrote after a sync: a JSON object whose `titles` is a list of strings."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    titles = data.get("titles") if isinstance(data, dict) else None
    return isinstance(titles, list) and all(isinstance(t, str) for t in titles)


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
        "  notebook-alchemy clean --records        forget past uploads and upload confirmations\n"
        "  notebook-alchemy clean --all --yes      cache, vaults and records, without asking\n"
    ),
)
@click.option(
    "--all", "clean_all", is_flag=True, help="Clear the cache, delete built vaults and local records."
)
@click.option("--cache", "clean_cache", is_flag=True, help="Clear the transcript cache.")
@click.option("--vaults", "clean_vaults", is_flag=True, help="Delete built compendium folders.")
@click.option(
    "--records",
    "clean_records",
    is_flag=True,
    help="Delete the records of past uploads and upload confirmations.",
)
@click.option("-y", "--yes", is_flag=True, help="Do not ask before deleting.")
@handle_errors
def clean_pipeline(
    clean_all: bool, clean_cache: bool, clean_vaults: bool, clean_records: bool, yes: bool
) -> None:
    """Remove the transcript cache, built vaults, or this tool's local records."""
    config = _config()
    engine = AlchemyEngine(config)
    do_vaults = clean_all or clean_vaults
    do_records = clean_all or clean_records
    do_cache = clean_all or clean_cache or not (do_vaults or do_records)

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
            if any((d / n).is_file() for n in _VAULT_MARKERS):
                targets.append(("vault", d))
            else:
                skipped.append(str(d))
    if do_records:
        # Records of past uploads (titles per notebook id) and the upload-confirmation hashes.
        records_dir = engine.sync_records_dir()
        if records_dir.is_dir():
            for f in sorted(records_dir.glob("*.json")):
                if _is_sync_record(f):
                    targets.append(("record", f))
                else:
                    skipped.append(str(f))
        if consent_file().is_file():
            targets.append(("record", consent_file()))
    for item in skipped:
        ui.warn(f"Skipped {esc(item)}: it does not look like Notebook Alchemy output.")
    if not targets:
        ui.success("Nothing to clean.")
        return

    cache_targets = [p for kind, p in targets if kind == "cache"]
    vault_targets = [p for kind, p in targets if kind == "vault"]
    record_targets = [p for kind, p in targets if kind == "record"]
    ask = (vault_targets or record_targets or (cache_targets and custom_cache)) and not yes
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
        if record_targets:
            ui.console.print(
                f"[bold]{len(record_targets)} local records will be deleted[/bold] (the next sync cannot "
                "remove sources an earlier sync uploaded, and the next upload asks for confirmation again)."
            )
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
                record(
                    "Compendiums build", ok, f"{built.total_compendiums} compendiums plus {INDEX_FILENAME}"
                )
            except AlchemyError as exc:
                record("Compendiums build", False, exc.message)

        if built and built.vault_directory:
            files = [
                f
                for f in built.vault_directory.glob("*.md")
                if f.name not in (INDEX_FILENAME, CATALOG_FILENAME)
            ]
            valid = sum(
                1
                for f in files
                if (t := f.read_text(encoding="utf-8")).startswith("---")
                and "domain:" in t
                and "## 2. Video Index" in t
            )
            catalog = (built.vault_directory / CATALOG_FILENAME).read_text(encoding="utf-8")
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
