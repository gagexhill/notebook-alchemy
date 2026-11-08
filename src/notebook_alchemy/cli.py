"""Command line interface for Notebook Alchemy.

Output follows Google's command-line conventions (gcloud): everyday words by default, detail behind
-v, "Warning:" and "Error:" prefixes on stderr, resource names in [brackets], next steps written as a
command to run, and confirmations asked as "Do you want to continue (y/N)?".
"""

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
from rich.table import Table

from . import __version__, ui
from .analyzer import NO_KEY_NOTE, DryRunReport
from .channel import CHANNEL_TABS, DEFAULT_TABS, video_id_from_url
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
from .gemini import THINKING_NOTE
from .synthesizer import (
    CATALOG_FILENAME,
    INDEX_FILENAME,
)
from .ui import esc, handle_errors

MOCK_NOTE = "This is sample data for trying the tool. It is never uploaded to NotebookLM."
NO_KEY_WARNING = (
    "No Gemini API key is set, so documents list titles and links without summaries. "
    "Add one with `notebook-alchemy setup`."
)
NOTEBOOKLM_URL = "https://notebooklm.google.com"


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


def _ask(question: str, *, default: bool = False) -> bool:
    """A yes/no question in gcloud's form: `Do you want to continue (y/N)?`."""
    return click.confirm(
        f"{question} ({'Y/n' if default else 'y/N'})?", default=default, show_default=False, prompt_suffix=" "
    )


def _plural(count: int, word: str) -> str:
    if count == 1:
        return f"{count} {word}"
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return f"{count} {word[:-1]}ies"
    return f"{count} {word}s"


def _money(usd: float) -> str:
    if usd <= 0:
        return "$0.00"
    return "less than $0.01" if usd < 0.005 else f"about ${usd:.2f}"


def _more_detail(_ctx: click.Context, _param: click.Parameter, value: int) -> int:
    """`-v` after a command works like `-v` before it: show detail (and -vv debug logs)."""
    if value:
        ui.UiState.verbose = max(ui.UiState.verbose, value)
        if not ui.UiState.quiet:
            logging.getLogger().setLevel(logging.DEBUG if ui.UiState.verbose > 1 else logging.INFO)
    return value


verbose_option = click.option(
    "-v",
    "--verbose",
    count=True,
    expose_value=False,
    callback=_more_detail,
    help="Show more detail (-vv adds debug logs).",
)

profile_option = click.option(
    "-p",
    "--profile",
    help="Name of the signed-in Google account to upload to (see `login`). Default: 'default'.",
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


def _who(identity: AccountIdentity) -> str:
    """The upload account, markup-safe. Unknown is said plainly, never guessed."""
    if identity.email:
        return ui.name(identity.email)
    if identity.storage:
        return "the Google account in the session file"
    return f"the Google account signed in to profile {ui.name(identity.profile)}"


def _where(identity: AccountIdentity) -> str:
    return f"session file {identity.storage}" if identity.storage else f"profile {identity.profile}"


def _show_upload_target(engine: AlchemyEngine) -> AccountIdentity:
    identity = engine.client.upload_account()
    limits = engine.client.plan_limits()
    extras = []
    if not identity.storage and identity.profile != "default":
        extras.append(f"profile {esc(identity.profile)}")
    if limits and limits.source_limit:
        extras.append(f"NotebookLM {esc(limits.plan)}, up to {limits.source_limit} sources per notebook")
    ui.info(f"Uploads go to {_who(identity)}" + (f" ({'; '.join(extras)})" if extras else "") + ".")
    ui.detail(f"  Account read from {esc(_where(identity))}.")
    if not (limits and limits.source_limit):
        ui.detail("  NotebookLM plan: unknown; the source limit is checked again at upload.")
    if not identity.email:
        reason = f" ({esc(identity.detail)})" if identity.detail else ""
        ui.warn(f"The sign-in did not record which Google account it is{reason}.", always=True)
        ui.next_command(
            "record it",
            f"notebook-alchemy login --profile {identity.profile} --browser-cookies chrome --account <email>",
        )
    return identity


def _confirm_first_upload(identity: AccountIdentity, yes: bool) -> None:
    """Ask before the first upload to a profile, and again if its account changes. --yes skips.

    An unknown account is asked about every time: two different unknown accounts look the same.
    """
    if yes or upload_confirmed(identity):
        return
    first = "an upload" if identity.email is None else "the first upload"
    if not _interactive():
        raise SessionError(
            f"This is {first} to {identity.display} in NotebookLM (profile '{identity.profile}'), "
            "and it needs your confirmation.",
            hint=(
                "Run the command in a terminal to confirm, or add --yes. To be asked only once, record "
                "the account: `notebook-alchemy login --browser-cookies chrome --account <email>`."
            ),
        )
    ui.console.print(f"\nNotebooks will be created in {_who(identity)}.")  # shown even with -q
    if not _ask("Do you want to continue"):
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
            f"The upload account changed while the documents were being written: it was "
            f"{confirmed.display}, now it is {now.display}. Nothing was uploaded.",
            hint="The documents are saved. Check the account with `notebook-alchemy status`, then "
            "upload them with `notebook-alchemy sync`.",
        )


def _show_gemini_cost(engine: AlchemyEngine) -> None:
    """What Gemini actually used, as Google reported it, priced at today's listed rates."""
    rows = engine.gemini.usage() if engine.gemini else []
    if not rows:
        return
    ui.info(f"  Gemini cost: {_money(sum(cost for *_rest, cost in rows))}")
    for model, tokens_in, tokens_out, cost in rows:
        ui.detail(
            f"  {esc(model)}: {tokens_in:,} input and {tokens_out:,} output tokens (${cost:.4f}; "
            "output includes thinking)"
        )


def _show_notes(notes: list[str]) -> None:
    """Notes that need the reader's attention are warnings; background notes are detail."""
    for note in notes:
        if note == NO_KEY_NOTE:
            continue  # the missing key is reported once, by the caller, with what to do about it
        if note == THINKING_NOTE:
            ui.detail(esc(note))  # the cost line already says "about"
        else:
            ui.warn(esc(note))


def _parse_tabs(_ctx: click.Context, _param: click.Parameter, value: str) -> tuple[str, ...]:
    tabs = tuple(dict.fromkeys(t.strip().lower() for t in value.split(",") if t.strip()))
    unknown = [t for t in tabs if t not in CHANNEL_TABS]
    if unknown or not tabs:
        raise click.BadParameter(f"use one or more of {', '.join(CHANNEL_TABS)}, separated by commas.")
    return tabs


tabs_option = click.option(
    "--tabs",
    default=",".join(DEFAULT_TABS),
    show_default=True,
    callback=_parse_tabs,
    help="Channel tabs to read: videos, streams, shorts (comma-separated). Ignored for playlists and videos.",
)


def _last_words(text: str, limit: int = 300) -> str:
    """The end of a tool's output on one line: the cause is usually last, after progress notes."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else "..." + flat[-limit:].split(" ", 1)[-1]


def _check_planned_sources(engine: AlchemyEngine, report: DryRunReport, max_per_vol: int) -> None:
    """Stop before writing, and before any Gemini spend, when the result cannot fit the plan."""
    limits = engine.client.plan_limits()
    if not limits or not limits.source_limit:
        return
    planned = sum(-(-len(v) // max_per_vol) for v in report.domain_videos.values()) + 2  # + index, catalog
    if planned > limits.source_limit:
        raise AlchemyError(
            f"This would make about {planned} sources, but a NotebookLM {limits.plan} notebook holds "
            f"up to {limits.source_limit}. Nothing was built or uploaded.",
            hint="Raise --max-per-vol for fewer, larger documents, or add --no-sync to build without "
            "uploading.",
        )


def _target(source: str | None, notebook: str | None, mock: bool) -> str:
    target = notebook or source
    if target:
        return target
    if mock:
        return "sample catalog"
    raise click.UsageError(
        "Give a YouTube channel, playlist or video URL (or an existing notebook), or add --mock "
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
        raise click.UsageError("Add --notebook NAME (the source's name) or --source-dir PATH.")
    candidate = engine.vault.compendium_dir(notebook)
    if candidate.is_dir():
        return candidate
    available = [str(p.parent.name) for p in engine.vault.existing_compendium_dirs()]
    raise VaultError(
        f"No documents found for '{notebook}' at {candidate}.",
        hint=(
            "Available sources: " + ", ".join(available) + ". Use --source-dir with one of them."
            if available
            else "Run `notebook-alchemy run <source>` first."
        ),
    )


def _cost_detail(report: DryRunReport) -> str:
    return (
        f"~{report.total_estimated_tokens:,} transcript tokens, estimated "
        f"${report.total_estimated_cost_usd:.4f} with {esc(report.model)}"
    )


@click.group(
    cls=OrderedGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    epilog=(
        "\b\nFirst run:\n"
        "  notebook-alchemy test                    check that everything installed correctly\n"
        "  notebook-alchemy setup                   add a Gemini key and choose the Google account\n"
        "  notebook-alchemy dry-run <youtube-url>   preview videos, topics and cost\n"
        "  notebook-alchemy run <youtube-url>       write the documents and upload them\n"
        "\nDocs: https://github.com/gagexhill/notebook-alchemy"
    ),
)
@click.version_option(version=__version__, prog_name="notebook-alchemy")
@click.option("-q", "--quiet", is_flag=True, help="Only print errors and final results.")
@click.option("-v", "--verbose", count=True, help="Show more detail (-vv adds debug logs).")
@click.option("--no-color", is_flag=True, help="Turn off colour (the NO_COLOR variable also works).")
@click.option("--debug", is_flag=True, help="Print full tracebacks for unexpected errors.")
def cli(quiet: bool, verbose: int, no_color: bool, debug: bool) -> None:
    """Notebook Alchemy: turn a YouTube channel into documents for Google NotebookLM.

    It finds every video, sorts them into topics, writes one document per topic (summarized with
    Gemini, or as a list of titles and links), and uploads them to a NotebookLM notebook, so a large
    channel fits in a few sources.
    """
    ui.configure(quiet=quiet, no_color=no_color or bool(os.getenv("NO_COLOR")), debug=debug, verbose=verbose)
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
        "  notebook-alchemy run @Hyperplexed --no-sync          write the documents, upload nothing\n"
        '  notebook-alchemy run <url> -N "CSS Course" --yes     name the notebook, skip questions\n'
        "  notebook-alchemy run --mock --no-sync                try the sample catalog\n"
    ),
)
@click.argument("source", required=False)
@click.option("-n", "--notebook", help="YouTube URL or existing notebook (instead of SOURCE).")
@click.option("-N", "--notebook-name", help="Name for the NotebookLM notebook.")
@click.option("--offline", is_flag=True, help="Don't use Gemini: group by title words, list titles only.")
@click.option("--mock", is_flag=True, help="Use the bundled sample catalog instead of a real source.")
@click.option("--limit", type=click.IntRange(min=1), help="Only look at the first N videos.")
@tabs_option
@click.option(
    "--max-per-vol",
    type=click.IntRange(min=1),
    default=DEFAULT_MAX_PER_VOLUME,
    show_default=True,
    help="Most videos per document; a bigger topic is divided into named subtopics.",
)
@click.option("--write/--preview-only", default=True, help="Write the documents (default) or only preview.")
@click.option("--sync/--no-sync", default=True, help="Upload to NotebookLM (default) or only write locally.")
@click.option("-y", "--yes", is_flag=True, help="Accept defaults without asking.")
@profile_option
@verbose_option
@handle_errors
def run_pipeline(
    source: str | None,
    notebook: str | None,
    notebook_name: str | None,
    offline: bool,
    mock: bool,
    limit: int | None,
    tabs: tuple[str, ...],
    max_per_vol: int,
    write: bool,
    sync: bool,
    yes: bool,
    profile: str | None,
) -> None:
    """Turn a YouTube channel, playlist or video into NotebookLM documents."""
    target = _target(source, notebook, mock)
    engine = _engine(profile)
    console = ui.console

    do_sync = sync and write and not mock  # sample data is never uploaded
    confirmed: AccountIdentity | None = None
    if do_sync:
        with console.status("Checking your NotebookLM sign-in..."):
            session = engine.client.check_session()
        if not session.ok:
            raise SessionError(
                session.detail,
                hint="Sign in with `notebook-alchemy login`, or add --no-sync to write the documents "
                "without uploading.",
            )
        ui.detail(f"Signed in; {_plural(session.notebooks, 'notebook')} visible.")
        confirmed = _show_upload_target(engine)
        _confirm_first_upload(confirmed, yes)

    total_steps = 3 if do_sync else 2
    live = engine.config.has_gemini_key and not offline

    ui.step(1, total_steps, "Finding videos")
    with ui.Reporter() as reporter:
        report = engine.analyzer_for(offline).generate_dry_run(
            target, use_mock=mock, limit=limit, tabs=tabs, max_per_section=max_per_vol, progress=reporter
        )
    if report.is_mock:
        ui.warn(MOCK_NOTE, always=True)
    ui.info(
        f"  {esc(report.notebook_title)}: {_plural(len(report.youtube_videos), 'video')} in "
        f"{_plural(len(report.domain_videos), 'topic')}"
    )
    if live:
        ui.info(f"  Estimated cost: {_money(report.total_estimated_cost_usd)}")
    ui.detail(f"  {_cost_detail(report)}; grouped by {report.cluster_method}")
    ui.detail(
        "  Documents: "
        + (f"summarized with {esc(engine.config.gemini_model)}" if live else "titles and links, no summaries")
    )
    if not live and not offline:
        ui.warn(NO_KEY_WARNING)
    _show_notes(report.notes)

    if do_sync:
        _check_planned_sources(engine, report, max_per_vol)

    final_name = notebook_name or report.notebook_title
    if do_sync and not notebook_name and not yes and sys.stdin.isatty():
        console.print()
        final_name = click.prompt("Notebook name", default=report.notebook_title).strip() or (
            report.notebook_title
        )

    ui.step(2, total_steps, "Writing documents")
    with ui.Reporter() as reporter:
        result = engine.run_distillation(
            force_offline=offline,
            max_videos_per_volume=max_per_vol,
            write_to_vault=write,
            dry_run=report,
            progress=reporter,
        )

    if ui.UiState.verbose:
        table = Table(title="Documents", header_style="bold green", title_justify="left")
        table.add_column("Topic", style="cyan", overflow="fold")
        table.add_column("Part", justify="right")
        table.add_column("Videos", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Contents")
        for c in result.compendiums:
            table.add_row(
                esc(c.domain),
                str(c.volume),
                str(len(c.titles)),
                f"~{c.token_count:,}",
                "summary" if c.is_live_synthesized else "titles and links",
            )
        console.print(table)
    partial = shortened = 0
    for w in result.warnings:
        if w in report.notes:
            continue
        if "had no transcript" in w or "were shortened" in w:  # per document; summed up after the loop
            partial += "had no transcript" in w
            shortened += "were shortened" in w
            ui.detail(esc(w))
        else:
            ui.warn(esc(w))
    if partial:
        ui.warn(
            f"{_plural(partial, 'summary')} left out videos that had no captions; their titles and links "
            "are still listed. Add -v to see which."
        )
    if shortened:
        ui.warn(
            f"Long captions were shortened to fit in {_plural(shortened, 'summary')}. Lower --max-per-vol "
            "to give each summary fewer videos."
        )

    count = _plural(result.total_compendiums, "document")
    if result.vault_directory:
        ui.success(f"Wrote {count} to {ui.name(_short(result.vault_directory))}.")
    else:
        ui.success(f"Prepared {count}; nothing was written (--preview-only).")
    ui.detail(
        f"  From {_plural(result.total_sources_scanned, 'video')}, ~{result.total_tokens:,} tokens; "
        f"the overview is {INDEX_FILENAME}."
    )
    _show_gemini_cost(engine)

    if do_sync:
        ui.step(3, total_steps, "Uploading to NotebookLM")
        if result.vault_directory is None or confirmed is None:  # confirmed is set whenever do_sync is
            raise AlchemyError("Nothing was written, so there is nothing to upload.")
        _same_account_as_confirmed(engine, confirmed)
        with ui.Reporter() as reporter:
            synced = engine.sync_to_notebooklm(final_name, result.vault_directory, progress=reporter)
        _show_sync_result(final_name, synced)
    elif write and not report.is_mock:
        ui.next_command("upload these documents", f'notebook-alchemy sync -n "{report.notebook_title}"')


def _show_sync_result(name: str, synced) -> None:
    ui.success(f"{'Created' if synced.created else 'Updated'} notebook {ui.name(name)}.")
    ui.detail(
        f"  Uploaded {_plural(synced.uploaded, 'file')}"
        + (f", replaced {synced.replaced} earlier copies" if synced.replaced else "")
        + (f", removed {synced.pruned} sources from an earlier run" if synced.pruned else "")
        + "."
    )
    if synced.freed_first:
        ui.detail("  Earlier copies were removed first, to stay within the plan's source limit.")
    ui.info(f"  Open it at {NOTEBOOKLM_URL}")


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
@click.option("-n", "--notebook", help="YouTube URL or existing notebook (instead of SOURCE).")
@click.option("--limit", type=click.IntRange(min=1), help="Only look at the first N videos.")
@tabs_option
@click.option("--offline", is_flag=True, help="Don't use Gemini: group by title words.")
@click.option("--mock", is_flag=True, help="Use the bundled sample catalog instead of a real source.")
@verbose_option
@handle_errors
def dry_run(
    source: str | None,
    notebook: str | None,
    limit: int | None,
    tabs: tuple[str, ...],
    offline: bool,
    mock: bool,
) -> None:
    """Preview videos, topics and cost. Writes and uploads nothing."""
    target = _target(source, notebook, mock)
    engine = _engine()
    console = ui.console
    ui.info(f"Looking at {esc(target)}...")
    with ui.Reporter() as reporter:
        report = engine.analyzer_for(offline).generate_dry_run(
            target, use_mock=mock, limit=limit, tabs=tabs, progress=reporter
        )
    if report.is_mock:
        ui.warn(MOCK_NOTE, always=True)

    origin = {"youtube": "YouTube", "notebooklm": "NotebookLM notebook", "mock": ""}[report.origin]
    total_videos = max(len(report.youtube_videos), 1)
    heading = f"\n[bold]{esc(report.notebook_title)}[/bold]"
    console.print(heading + (f" [dim]({origin})[/dim]" if origin else ""))
    console.print(
        f"  {_plural(len(report.youtube_videos), 'video')} in {_plural(len(report.domain_videos), 'topic')}"
        + (
            f", plus {_plural(len(report.other_sources), 'other source')} that are not videos"
            if report.other_sources
            else ""
        )
    )
    estimate = _money(report.total_estimated_cost_usd)
    if engine.config.has_gemini_key and not offline:
        console.print(f"  Estimated Gemini cost: {estimate}")
    else:
        console.print(f"  Cost: free without Gemini (titles and links only). With a Gemini key: {estimate}")
    ui.detail(
        f"  Transcript tokens: ~{report.total_estimated_tokens:,}; "
        f"input ${report.estimated_input_cost_usd:.4f}, "
        f"output ${report.estimated_synthesis_output_cost_usd:.4f} with {esc(report.model)}"
    )
    ui.detail(
        f"  Gemini requests: ~{report.estimated_gemini_requests} (the free tier allows a limited "
        f"number per day); grouped by {report.cluster_method}"
    )

    table = Table(title="\nTopics", header_style="bold green", title_justify="left")
    table.add_column("Topic", style="cyan", overflow="fold")
    table.add_column("Videos", justify="right")
    table.add_column("Share")
    table.add_column("Examples", style="dim", overflow="fold")
    for domain, vids in sorted(report.domain_videos.items(), key=lambda kv: -len(kv[1])):
        share = len(vids) / total_videos
        samples = "; ".join(esc(shorten(v.title, 50, placeholder="…")) for v in vids[:2])
        table.add_row(esc(domain), str(len(vids)), f"{'█' * max(1, round(share * 12))} {share:.0%}", samples)
    console.print(table)
    if not engine.config.has_gemini_key and not offline:
        ui.warn(NO_KEY_WARNING)
    _show_notes(report.notes)
    if not report.is_mock:
        ui.next_command("write the documents and upload them", f"notebook-alchemy run {target}")


# --- status / login ------------------------------------------------------------------------------


@cli.command(name="status")
@profile_option
@verbose_option
@handle_errors
def status(profile: str | None) -> None:
    """Show whether everything is ready: sign-in, Gemini key and where documents are saved."""
    engine = _engine(profile)
    facts = engine.status()
    identity = engine.client.upload_account()
    has_cli = not str(facts["notebooklm_cli"]).startswith("not found")
    has_session = not str(facts["session_file"]).startswith("none")
    has_key = facts["gemini_api_key"] == "set"
    console = ui.console

    def line(ok: bool, text: str) -> None:
        console.print(("[green]✓[/green] " if ok else "[red]✗[/red] ") + text)

    if not has_cli:
        line(False, "The notebooklm tool is not installed.")
    if not has_session:
        line(False, "Not signed in to NotebookLM.")
    else:
        where = f" (profile {esc(identity.profile)})" if identity.profile != "default" else ""
        if identity.email:
            line(True, f"Uploads go to {ui.name(identity.email)}{where}.")
        else:
            line(False, f"Signed in to NotebookLM{where}, but the Google account was not recorded.")
    if has_key:
        line(True, "AI summaries are on (a Gemini API key is set).")
    else:
        line(False, "AI summaries are off (no Gemini API key), so documents list titles and links.")
    console.print(f"  Documents are saved in {ui.name(facts['vault_path'])}")

    if ui.UiState.verbose:
        table = Table(title="\nSettings", header_style="bold magenta", title_justify="left")
        table.add_column("", width=1)
        table.add_column("Setting", style="bold")
        table.add_column("Value", overflow="fold")
        rows = [
            (has_cli, "notebooklm tool", esc(facts["notebooklm_cli"])),
            (None, "NotebookLM profile", esc(facts["notebooklm_profile"])),
            (has_session, "Sign-in file", esc(facts["session_file"])),
            (bool(identity.email) if has_session else None, "Upload account", _who(identity)),
            (has_key, "Gemini API key", esc(facts["gemini_api_key"])),
            (None, "Gemini model", esc(facts["gemini_model"])),
            (None, "Documents folder", esc(facts["vault_path"])),
            (None, "Caption cache", esc(facts["cache_dir"])),
            (None, "Parallel downloads", esc(facts["max_concurrency"])),
            (None, "Environment", esc(facts["environment"])),
        ]
        for ok, setting, value in rows:
            glyph = "[green]✓[/green]" if ok else "[red]✗[/red]" if ok is False else "[dim]-[/dim]"
            table.add_row(glyph, setting, value)
        console.print(table)
        if has_key:
            console.print(f"[dim]{esc(KEY_BILLING_NOTE)}[/dim]")

    if not has_cli:
        ui.next_command("install the notebooklm tool", "pip install notebooklm-py")
    if not has_session:
        ui.next_command("sign in", "notebook-alchemy login")
    elif not identity.email:
        ui.next_command(
            "record the Google account", "notebook-alchemy login --browser-cookies chrome --account <email>"
        )
    if not has_key:
        ui.next_command("turn on AI summaries", "notebook-alchemy setup")
    if not ui.UiState.verbose:
        ui.info("\n[dim]For every setting, run: notebook-alchemy status -v[/dim]")


def _run_login(cmd: list[str], waiting: str, answer: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run `notebooklm login` with its output captured. Its stdin gets `answer`, or is closed, so a
    question it asks fails at once instead of waiting unseen behind the spinner."""
    io: dict[str, object] = {"input": answer} if answer is not None else {"stdin": subprocess.DEVNULL}
    try:
        with ui.console.status(waiting):
            return subprocess.run(  # type: ignore[call-overload]
                cmd, check=False, capture_output=True, text=True, encoding="utf-8", **io
            )
    except FileNotFoundError as exc:
        raise NotebookLMError(
            "The notebooklm tool is not installed.",
            hint="Install it with: pip install notebooklm-py",
        ) from exc


def _sign_in(
    client: NotebookLMClient,
    *,
    browser: str = "chromium",
    browser_cookies: str | None = None,
    account: str | None = None,
    timeout: int = 300,
    fresh: bool = False,
) -> AccountIdentity:
    """Run `notebooklm login` for this profile, then read back which account it recorded.

    notebooklm's own progress lines (profile, cookies, file paths) are captured, not shown: the
    result is reported in one line, and the captured text explains a failure.
    """
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
    override = client.storage_override()
    ui.detail(
        f"Signing in through {esc(f'session file {override}' if override else f'profile {client.profile}')}."
    )
    if browser_cookies:
        cmd += ["--browser-cookies", browser_cookies]
        if account:
            cmd += ["--account", account]
        ui.info(f"Using your {esc(browser_cookies.split('::', 1)[0].title())} sign-in...")
        waiting = "Reading the sign-in..."
    else:
        cmd += ["--browser", browser, "--browser-timeout", str(timeout)]
        if fresh:
            cmd.append("--fresh")
        ui.info("A browser window will open. Sign in with the Google account you want to upload to.")
        waiting = "Waiting for you to finish signing in..."
    proc = _run_login(cmd, waiting)
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if proc.returncode != 0 and " already has " in output and "Overwrite it with" in output:
        # notebooklm asks before replacing a profile's account. Its question is captured (stdin is
        # closed, so it cannot hang unseen); it is asked here instead, and answered on its behalf.
        where = f"profile '{client.profile}'" if not override else "the session file"
        if not _interactive():
            raise SessionError(
                f"{where.capitalize()} is already signed in to a different Google account. "
                "Nothing was changed.",
                hint="Run the command in a terminal to replace it, or sign in to a new profile with "
                "--profile <name>.",
            )
        ui.console.print(f"{where.capitalize()} is already signed in to a different Google account.")
        if not _ask("Do you want to replace it"):
            raise AlchemyError(
                "Cancelled. Nothing was changed.", hint="Use --profile <name> to keep both accounts."
            )
        proc = _run_login(cmd, waiting, answer="y\n")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
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
            + (f" {_last_words(detail)}" if detail else ""),
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
    with ui.console.status("Checking the sign-in..."):
        result = client.check_session()
    if not result.ok:
        raise SessionError(
            f"Sign-in finished but the check that it works failed: {result.detail}",
            hint="Run `notebook-alchemy login` again.",
        )
    ui.success(f"You are now signed in as {_who(identity)}.")
    return identity


@cli.command(
    name="login",
    epilog=(
        "\b\nExamples:\n"
        "  notebook-alchemy login                        sign in through a browser window\n"
        "  notebook-alchemy login --browser-cookies chrome  reuse a Chrome sign-in, no window\n"
        "  notebook-alchemy login --browser-cookies chrome --account you@example.com --profile personal\n"
        "                                                pick one of several signed-in accounts\n"
        "  notebook-alchemy login --check                is the sign-in still working?\n"
    ),
)
@click.option(
    "--browser",
    default="chromium",
    show_default=True,
    type=click.Choice(["chromium", "chrome", "msedge"]),
    help="Browser to open for signing in.",
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
@click.option("--fresh", is_flag=True, help="Open the sign-in window without earlier browser data.")
@click.option("--check", is_flag=True, help="Check the saved sign-in without signing in again.")
@click.option(
    "--timeout",
    type=click.IntRange(min=30),
    default=300,
    show_default=True,
    help="Seconds to wait for you to finish signing in.",
)
@profile_option
@verbose_option
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
    """Sign in to NotebookLM with the Google account to upload to."""
    client = NotebookLMClient(_config(profile))
    if check:
        with ui.console.status("Checking your NotebookLM sign-in..."):
            result = client.check_session()
        if not result.ok:
            raise SessionError(result.detail, hint="Sign in again with `notebook-alchemy login`.")
        identity = client.upload_account()
        ui.success(f"You are signed in as {_who(identity)} ({_plural(result.notebooks, 'notebook')}).")
        ui.detail(f"  Account read from {esc(_where(identity))}.")
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
        "\b\nRun it again any time to change the key, the account or the folder.\n"
        "Example:\n"
        "  notebook-alchemy setup\n"
    ),
)
@verbose_option
@handle_errors
def setup() -> None:
    """Guided setup: Gemini API key, Google account and documents folder."""
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

    ui.step(1, 3, "AI summaries (optional)")
    ui.info("  Gemini writes a summary for each document. Without a key, documents list titles and links.")
    ui.info(
        "  " + ("A Gemini API key is saved." if config.has_gemini_key else "No Gemini API key is saved yet.")
    )
    ui.info("  Get a free key at https://aistudio.google.com/apikey")
    ui.detail(f"  {esc(KEY_BILLING_NOTE)}")
    while True:
        entered = click.prompt(
            "  Paste a key, or press Enter to skip",
            default="",
            show_default=False,
            hide_input=True,
        ).strip()
        if not entered or is_usable_api_key(entered):
            break
        ui.warn("That does not look like an API key. Try again, or press Enter to skip.")
    if entered:
        save("GEMINI_API_KEY", entered)
        ui.success("Key saved.")
        ui.detail(f"  Saved to {esc(_short(env_file))}; the key is never shown.")

    ui.step(2, 3, "Where to upload")
    ui.info(
        "  Choose the Google account your notebooks go to. It can differ from the account that made "
        "the Gemini key."
    )
    while True:
        try:
            profile = validate_profile_name(
                click.prompt(
                    "  A short name for this account, like personal or work",
                    default=config.notebooklm_profile,
                ),
                source="profile",
            )
            break
        except ConfigError as exc:
            ui.warn(f"{esc(exc.message)} {esc(exc.hint or '')}")
    config.notebooklm_profile = profile
    client = NotebookLMClient(config)
    identity = client.upload_account()
    if identity.email:
        ui.info(f"  Signed in as {_who(identity)}.")
    choice = click.prompt(
        "  Keep this account (keep), open a sign-in window (window), or reuse a browser's sign-in (cookies)",
        type=click.Choice(["keep", "window", "cookies"]),
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
            "  Google account to upload to", default=emails[0] if len(emails) == 1 else None
        ).strip()
        identity = _sign_in(client, browser_cookies=browser_cookies, account=account)
    elif choice == "window":
        _sign_in(client, fresh=_ask("  Start without earlier browser data", default=True))
        identity = client.upload_account()
    save("ALCHEMY_NOTEBOOKLM_PROFILE", profile)

    ui.step(3, 3, "Where to save documents")
    # The default is what .env already says (or "vault"), never the resolved path with the home folder.
    vault = click.prompt(
        "  Folder for your documents",
        default=os.getenv("ALCHEMY_VAULT_PATH", "").strip() or "vault",
    )
    vault_path = Path(vault).expanduser().resolve()
    # Saved as typed (relative or ~/...), so .env does not carry the user's home folder name.
    save("ALCHEMY_VAULT_PATH", vault.strip())

    ui.success("Setup is complete.")
    ui.info(f"  Uploads go to {_who(identity)}.")
    ui.info("  AI summaries are " + ("on." if entered or config.has_gemini_key else "off (no Gemini key)."))
    ui.info(f"  Documents are saved in {ui.name(vault)}.")
    ui.detail(f"  {esc(vault_path)}; saved {esc(', '.join(saved))} to {esc(_short(env_file))}.")
    ui.next_command("preview a channel", "notebook-alchemy dry-run <youtube-url>")


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
    help="NotebookLM notebook to create or update (also picks the documents folder).",
)
@click.option(
    "--source-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Folder of documents to upload (default: the folder named after --notebook).",
)
@click.option("-y", "--yes", is_flag=True, help="Don't ask before the first upload to an account.")
@profile_option
@verbose_option
@handle_errors
def sync_to_notebooklm(notebook: str | None, source_dir: Path | None, yes: bool, profile: str | None) -> None:
    """Upload written documents to a notebook, replacing earlier copies."""
    engine = _engine(profile)
    directory = _source_dir(engine, notebook, source_dir)
    name = notebook or directory.parent.name
    with ui.console.status("Checking your NotebookLM sign-in..."):
        session = engine.client.check_session()
    if not session.ok:
        raise SessionError(session.detail, hint="Sign in with `notebook-alchemy login`, then run sync again.")
    _confirm_first_upload(_show_upload_target(engine), yes)
    ui.detail(f"Uploading {esc(_short(directory))}.")
    with ui.Reporter() as reporter:
        result = engine.sync_to_notebooklm(name, directory, progress=reporter)
    _show_sync_result(name, result)


@cli.command(
    name="append",
    epilog=(
        "\b\nExample:\n"
        '  notebook-alchemy append -n Hyperplexed --title "New video" '
        "--url https://youtu.be/dQw4w9WgXcQ\n"
    ),
)
@click.option("-n", "--notebook", help="Source name; picks the documents folder.")
@click.option("--source-dir", type=click.Path(file_okay=False, path_type=Path), help="Documents folder.")
@click.option("--title", required=True, help="Title of the new video.")
@click.option("--url", required=True, help="URL of the new video.")
@click.option("--topic", help="Topic to file it under (default: chosen automatically).")
@verbose_option
@handle_errors
def append_video(
    notebook: str | None, source_dir: Path | None, title: str, url: str, topic: str | None
) -> None:
    """Add one new video to its topic's document without rewriting everything."""
    engine = _engine()
    directory = _source_dir(engine, notebook, source_dir)
    name = notebook or directory.parent.name
    video = {"id": video_id_from_url(url), "title": title, "url": url}
    result = engine.append_incremental_sources([video], directory, domain=topic, notebook_title=name)
    if result.added:
        ui.success(f"Added {ui.name(title)} and updated {_plural(len(result.updated_files), 'document')}.")
        ui.detail(f"  Updated: {esc(', '.join(result.updated_files))}")
        ui.next_command("upload the change", f'notebook-alchemy sync -n "{name}"')
    else:
        ui.info("That video is already in the catalog, so nothing changed.")


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
        "  notebook-alchemy clean                  clear downloaded captions\n"
        "  notebook-alchemy clean --vaults         delete written documents (asks first)\n"
        "  notebook-alchemy clean --records        forget past uploads and upload confirmations\n"
        "  notebook-alchemy clean --all --yes      all of the above, without asking\n"
    ),
)
@click.option(
    "--all", "clean_all", is_flag=True, help="Clear captions, delete documents and forget past uploads."
)
@click.option("--cache", "clean_cache", is_flag=True, help="Clear downloaded captions.")
@click.option("--vaults", "clean_vaults", is_flag=True, help="Delete written document folders.")
@click.option(
    "--records",
    "clean_records",
    is_flag=True,
    help="Forget past uploads and upload confirmations.",
)
@click.option("-y", "--yes", is_flag=True, help="Don't ask before deleting.")
@verbose_option
@handle_errors
def clean_pipeline(
    clean_all: bool, clean_cache: bool, clean_vaults: bool, clean_records: bool, yes: bool
) -> None:
    """Remove downloaded captions, written documents, or this tool's upload records."""
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
                f"{_plural(len(cache_targets), 'caption file')} in a custom cache folder "
                f"{ui.name(config.cache_dir)} will be deleted."
            )
        if vault_targets:
            ui.console.print("These document folders will be deleted:")
            for p in vault_targets:
                ui.console.print(f"  {esc(p)}")
        if record_targets:
            ui.console.print(
                f"{len(record_targets)} local records will be deleted. Later syncs will not remove sources "
                "that earlier syncs uploaded, and the next upload asks you to confirm the account again."
            )
        if not _ask("Do you want to continue"):
            ui.info("Cancelled. Nothing was deleted.")
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
            f"{_plural(len(failed), 'item')} could not be deleted: " + "; ".join(failed[:3]),
            hint="Close programs using those files and run the command again.",
        )
    ui.success(f"Removed {_plural(len(targets), 'item')} and freed {ui.human_bytes(freed)}.")


@cli.command(name="test")
@verbose_option
@handle_errors
def run_automated_test() -> None:
    """Check that everything installed correctly, using sample data. No network, nothing kept."""
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
            record("Sample videos load", ok, f"{len(report.youtube_videos)} sample videos")
            domains = len(report.domain_videos)
            record("Videos sort into topics", domains >= 3, f"{domains} topics (title words)")
        except AlchemyError as exc:
            record("Sample videos load", False, exc.message)

        built = None
        if report:
            try:
                built = engine.run_distillation(force_offline=True, dry_run=report, write_to_vault=True)
                ok = built.total_compendiums >= 3 and bool(built.index_path and built.index_path.exists())
                record(
                    "Documents are written", ok, f"{built.total_compendiums} documents plus {INDEX_FILENAME}"
                )
            except AlchemyError as exc:
                record("Documents are written", False, exc.message)

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
            record("Documents are valid", ok, f"{valid}/{len(files)} documents, {rows} catalog rows")

    left_behind = cwd_vault.exists() and not existed_before
    record("Nothing written outside the temp folder", not left_behind, "working directory untouched")

    elapsed = time.monotonic() - started
    passed = all(ok for _n, ok, _d in results)
    if ui.UiState.verbose or not passed:
        table = Table(title="Self-test", header_style="bold", title_justify="left")
        table.add_column("", width=1)
        table.add_column("Check", style="bold")
        table.add_column("Result", style="dim", overflow="fold")
        for check, ok, detail in results:
            table.add_row("[green]✓[/green]" if ok else "[red]✗[/red]", check, esc(detail))
        ui.console.print(table)
    if passed:
        ui.success(f"All {len(results)} checks passed in {elapsed:.1f}s. Notebook Alchemy is ready to use.")
    else:
        failed = sum(1 for _n, ok, _d in results if not ok)
        ui.err_console.print(
            f"[bold red]Error:[/bold red] {failed} of {len(results)} checks failed.\n"
            "[bold]Next:[/bold] Run `notebook-alchemy --debug test` and include the output in a bug report."
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
