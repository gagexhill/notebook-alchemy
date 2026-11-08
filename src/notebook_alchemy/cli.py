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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from . import __version__, ui
from .channel import CHANNEL_TABS, DEFAULT_TABS
from .client import (
    AccountIdentity,
    NotebookLMClient,
    consent_file,
    record_upload_consent,
    upload_confirmed,
)
from .config import (
    COOKIE_BROWSERS,
    LOG_LEVELS,
    AlchemyConfig,
    is_usable_api_key,
    update_dotenv,
    validate_profile_name,
)
from .engine import AlchemyEngine, BuildReport
from .errors import AlchemyError, ConfigError, NotebookLMError, SessionError
from .ui import esc, handle_errors
from .writer import OVERVIEW_TITLE

MOCK_NOTE = "This is sample data for trying the tool. It is never uploaded to NotebookLM."
NO_KEY_WARNING = (
    "No Gemini API key is set, so the books hold every video's words without AI sorting or labels. "
    "Add one with `notebook-alchemy setup`."
)
NOTEBOOKLM_URL = "https://notebooklm.google.com"


class OrderedGroup(click.Group):
    """Lists commands in the order they are defined, which follows a first-run path."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return [name for name, cmd in self.commands.items() if not cmd.hidden]


SETUP_KEYS = ("GEMINI_API_KEY", "ALCHEMY_NOTEBOOKLM_PROFILE")
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
            hint="Nothing was uploaded. Check the account with `notebook-alchemy status`, then run "
            "the same command again; the work already done is reused.",
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


def _source_or_mock(source: str | None, mock: bool) -> str:
    if source or mock:
        return source or "sample catalog"
    raise click.UsageError(
        "Give a YouTube channel, playlist or video URL, or add --mock to try the sample catalog.\n\n"
        "Example: notebook-alchemy dry-run https://www.youtube.com/@Hyperplexed"
    )


def _short(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _removed(message: str) -> Callable[[click.Context, click.Parameter, Any], Any]:
    """A callback for a flag that no longer exists: say what replaced it."""

    def callback(_ctx: click.Context, _param: click.Parameter, value: Any) -> Any:
        if value:
            raise click.UsageError(message)
        return value

    return callback


@click.group(
    cls=OrderedGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    epilog=(
        "\b\nFirst run:\n"
        "  notebook-alchemy test                    check that everything installed correctly\n"
        "  notebook-alchemy setup                   add a Gemini key and choose the Google account\n"
        "  notebook-alchemy dry-run <youtube-url>   preview videos, words and cost\n"
        "  notebook-alchemy run <youtube-url>       build the topic books and upload them\n"
        "\nDocs: https://github.com/gagexhill/notebook-alchemy"
    ),
)
@click.version_option(version=__version__, prog_name="notebook-alchemy")
@click.option("-q", "--quiet", is_flag=True, help="Only print errors and final results.")
@click.option("-v", "--verbose", count=True, help="Show more detail (-vv adds debug logs).")
@click.option("--no-color", is_flag=True, help="Turn off colour (the NO_COLOR variable also works).")
@click.option("--debug", is_flag=True, help="Print full tracebacks for unexpected errors.")
def cli(quiet: bool, verbose: int, no_color: bool, debug: bool) -> None:
    """Notebook Alchemy: a YouTube channel's words, as topic books in a NotebookLM notebook.

    It reads what every video says, keeps each point once, sorts the points into topics and
    sections with Gemini, drops or marks what is out of date, and uploads one book per topic.
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
        '  notebook-alchemy run @Hyperplexed -N "CSS Course" --yes   name the notebook, skip questions\n'
        "  notebook-alchemy run @Hyperplexed --save books           write the books here, upload nothing\n"
        "  notebook-alchemy run --mock --save books                 try the sample catalog\n"
        "\nRun it again any time: only new videos cost anything, and the notebook is updated in place.\n"
    ),
)
@click.argument("source", required=False)
@click.option("-N", "--notebook-name", help="Name for the NotebookLM notebook (default: the AI's title).")
@click.option(
    "--save",
    "save_dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Write the books to this folder instead of uploading.",
)
@click.option("--limit", type=click.IntRange(min=1), help="Only read the first N videos.")
@tabs_option
@click.option("--offline", is_flag=True, help="No Gemini: every video's words, sorted by title words.")
@click.option("--mock", is_flag=True, help="Use the bundled sample catalog instead of a real channel.")
@click.option(
    "--now", is_flag=True, help="Use standard Gemini calls instead of batch mode (faster, full price)."
)
@click.option("--resort", is_flag=True, help="Rebuild the topics and sections from scratch.")
@click.option(
    "--cookies",
    "cookies_browser",
    type=click.Choice(COOKIE_BROWSERS),
    help="Let yt-dlp use this browser's YouTube sign-in when captions are blocked.",
)
@click.option("-y", "--yes", is_flag=True, help="Accept defaults without asking.")
@click.option(
    "--no-sync",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_removed("--no-sync was removed: use --save DIR to keep the books locally without uploading."),
)
@click.option(
    "--max-per-vol",
    hidden=True,
    expose_value=False,
    callback=_removed(
        "--max-per-vol was removed: books are sized automatically, up to NotebookLM's 500,000 words "
        "per source."
    ),
)
@click.option(
    "--preview-only",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_removed("--preview-only was removed: use `notebook-alchemy dry-run`."),
)
@click.option(
    "-n",
    "--notebook",
    "old_notebook",
    hidden=True,
    expose_value=False,
    callback=_removed(
        "-n/--notebook was removed: give the YouTube link as the argument, and name the notebook with -N."
    ),
)
@click.option(
    "--sync",
    "old_sync",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_removed("--sync was removed: `run` uploads by default; --save DIR keeps a local copy instead."),
)
@click.option(
    "--write",
    "old_write",
    is_flag=True,
    hidden=True,
    expose_value=False,
    callback=_removed(
        "--write was removed: `run` uploads by default; --save DIR keeps a local copy instead."
    ),
)
@profile_option
@verbose_option
@handle_errors
def run_pipeline(
    source: str | None,
    notebook_name: str | None,
    save_dir: Path | None,
    limit: int | None,
    tabs: tuple[str, ...],
    offline: bool,
    mock: bool,
    now: bool,
    resort: bool,
    cookies_browser: str | None,
    yes: bool,
    profile: str | None,
) -> None:
    """Build topic books from a YouTube channel, playlist or video, and upload them to NotebookLM."""
    target = _source_or_mock(source, mock)
    config = _config(profile)
    if cookies_browser:
        config.cookies_browser = cookies_browser
    engine = AlchemyEngine(config)
    upload = save_dir is None and not mock  # sample data is never uploaded
    if mock and save_dir is None:
        raise click.UsageError("Sample data is never uploaded: add --save DIR to see the books it makes.")

    confirmed: AccountIdentity | None = None
    if upload:
        with ui.console.status("Checking your NotebookLM sign-in..."):
            session = engine.client.check_session()
        if not session.ok:
            raise SessionError(
                session.detail,
                hint="Sign in with `notebook-alchemy login`, or add --save DIR to keep the books locally.",
            )
        ui.detail(f"Signed in; {_plural(session.notebooks, 'notebook')} visible.")
        confirmed = _show_upload_target(engine)
        _confirm_first_upload(confirmed, yes)
    if mock:
        ui.warn(MOCK_NOTE, always=True)
    if not engine.config.has_gemini_key and not offline and not mock:
        ui.warn(NO_KEY_WARNING)

    total = 3 if upload else 2
    ui.step(1, total, "Reading every video")
    with ui.Reporter() as reporter:
        report = engine.build(
            target,
            limit=limit,
            tabs=tabs,
            mock=mock,
            offline=offline,
            resort=resort,
            use_batch=not now,
            progress=reporter,
        )
    _show_build(report, engine)

    ui.step(2, total, "Writing the books")
    words = sum(b.words for b in report.books)
    topics = [b for b in report.books if b.title != OVERVIEW_TITLE]
    ui.success(f"Wrote {_plural(len(topics), 'topic book')} and an overview, {words:,} words in all.")
    for book in topics:
        ui.detail(f"  {esc(book.title)}: {book.words:,} words")
    _show_gemini_cost(engine)

    if save_dir is not None:
        paths = engine.save_books(report.books, save_dir)
        ui.success(f"Saved {_plural(len(paths), 'file')} to {ui.name(_short(save_dir))}.")
        return

    if report.waiting:
        # Uploading now would replace the notebook's books with ones missing these videos.
        ui.warn(
            f"{_plural(len(report.waiting), 'video')} still wait for Gemini, so nothing was uploaded and "
            "the notebook is unchanged. Run the same command again to finish; the work done is kept.",
            always=True,
        )
        return

    ui.step(3, total, "Uploading to NotebookLM")
    if confirmed is None:  # set whenever uploading
        raise AlchemyError("Nothing to upload to.")
    title = notebook_name or report.library.notebook_title
    if not notebook_name and not yes and _interactive():
        ui.console.print()
        title = click.prompt("Notebook name", default=title).strip() or title
    _same_account_as_confirmed(engine, confirmed)
    with ui.Reporter() as reporter:
        synced = engine.publish(
            title, report.books, channel_key=None if notebook_name else target, progress=reporter
        )
    _show_sync_result(title, synced)


def _show_build(report: BuildReport, engine: AlchemyEngine) -> None:
    videos = len(report.videos)
    ui.info(f"  {esc(report.channel)}: read {len(report.read)} of {_plural(videos, 'video')}")
    for route, count in sorted(report.by_source.items()):
        ui.detail(f"  {route}: {count}")
    if report.used_ai:
        ui.info(
            f"  Labelled {report.labelled} new; {_plural(len(report.waiting), 'video')} wait for the next run"
            if report.waiting
            else f"  Labelled {report.labelled} new videos (the rest came from earlier runs)"
        )
    lib = report.library
    passages = sum(len(s.passages) for t in lib.topics for s in t.sections)
    ui.info(f"  {_plural(passages, 'passage')} in {_plural(len(lib.topics), 'topic')}")
    if lib.removed_duplicates or lib.removed_outdated:
        ui.info(
            f"  Kept once: {_plural(lib.removed_duplicates, 'repeated passage')}; "
            f"left out as outdated: {len(lib.removed_outdated)}"
        )
    for note in report.notices:
        ui.warn(esc(note), always=True)


def _show_sync_result(name: str, synced) -> None:
    ui.success(f"{'Created' if synced.created else 'Updated'} notebook {ui.name(name)}.")
    ui.detail(
        f"  Uploaded {_plural(synced.uploaded, 'book')}"
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
        "  notebook-alchemy dry-run @Hyperplexed --limit 50\n"
        "  notebook-alchemy dry-run --mock\n"
    ),
)
@click.argument("source", required=False)
@click.option("--limit", type=click.IntRange(min=1), help="Only look at the first N videos.")
@tabs_option
@click.option("--mock", is_flag=True, help="Use the bundled sample catalog instead of a real channel.")
@verbose_option
@handle_errors
def dry_run(source: str | None, limit: int | None, tabs: tuple[str, ...], mock: bool) -> None:
    """Preview the videos, the words already read, and the cost. Spends nothing, uploads nothing."""
    target = _source_or_mock(source, mock)
    engine = _engine()
    ui.info(f"Looking at {esc(target)}...")
    channel, videos = engine.scan(target, limit=limit, tabs=tabs, mock=mock)
    if mock:
        ui.warn(MOCK_NOTE, always=True)
    est = engine.estimate(videos, mock=mock)
    console = ui.console
    console.print(f"\n[bold]{esc(channel)}[/bold]")
    console.print(f"  {_plural(est.videos, 'video')}; words already read for {est.videos_known}")
    console.print(f"  About {est.words_estimated:,} words in all")
    if engine.config.has_gemini_key and not mock:
        left = est.videos - est.labelled
        console.print(
            f"  Estimated Gemini cost: {_money(est.cost_usd)} for {_plural(left, 'video')} not labelled yet"
        )
        ui.detail(f"  Model {esc(est.model)}; batch mode halves it when the key's project has billing.")
    else:
        console.print("  Cost: free without Gemini (every video's words, sorted by title words)")
        if not mock:
            ui.warn(NO_KEY_WARNING)
    if not mock:
        ui.next_command("build the books and upload them", f"notebook-alchemy run {target}")


# --- status / login ------------------------------------------------------------------------------


@cli.command(name="status")
@profile_option
@verbose_option
@handle_errors
def status(profile: str | None) -> None:
    """Show whether everything is ready: sign-in and Gemini key."""
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
        line(True, "AI sorting is on (a Gemini API key is set).")
    else:
        line(
            False,
            "AI sorting is off (no Gemini API key): the books hold every video's words, sorted by title.",
        )

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
            (None, "Working files", esc(facts["cache_dir"])),
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
        ui.next_command("turn on AI sorting", "notebook-alchemy setup")
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
        "\b\nRun it again any time to change the key or the account.\nExample:\n  notebook-alchemy setup\n"
    ),
)
@verbose_option
@handle_errors
def setup() -> None:
    """Guided setup: Gemini API key and the Google account to upload to."""
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

    ui.step(1, 2, "AI sorting (optional)")
    ui.info(
        "  Gemini picks what is worth keeping and sorts it into topics. Without a key, every video's "
        "words are kept, sorted by title."
    )
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

    ui.step(2, 2, "Where to upload")
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

    ui.success("Setup is complete.")
    if client.session_file() is None:
        ui.info("  Not signed in to NotebookLM yet.")
        ui.next_command("sign in", f"notebook-alchemy login --profile {profile}")
    else:
        ui.info(f"  Uploads go to {_who(identity)}.")
    ui.info("  AI sorting is " + ("on." if entered or config.has_gemini_key else "off (no Gemini key)."))
    ui.detail(f"  Saved {esc(', '.join(saved))} to {esc(_short(env_file))}.")
    ui.next_command("preview a channel", "notebook-alchemy dry-run <youtube-url>")


# --- removed commands ----------------------------------------------------------------------------


@cli.command(
    name="sync", hidden=True, context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
def removed_sync() -> None:
    raise click.UsageError(
        "`sync` was removed: run `notebook-alchemy run <youtube-url>` again; it only uploads what changed."
    )


@cli.command(
    name="append", hidden=True, context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
def removed_append() -> None:
    raise click.UsageError(
        "`append` was removed: run `notebook-alchemy run <youtube-url>` again; new videos are added "
        "and old ones cost nothing."
    )


# --- clean / test --------------------------------------------------------------------------------


def _json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_sync_record(path: Path) -> bool:
    """A record this tool wrote after an upload: a JSON object whose `titles` is a list of strings."""
    data = _json(path)
    titles = data.get("titles") if isinstance(data, dict) else None
    return isinstance(titles, list) and all(isinstance(t, str) for t in titles)


def _is_cache_record(path: Path) -> bool:
    """True for a caption cache file this tool wrote (JSON object with a video_id)."""
    data = _json(path)
    return isinstance(data, dict) and "video_id" in data


def _is_label_record(path: Path) -> bool:
    data = _json(path)
    return isinstance(data, dict) and "segments" in data and "key" in data


def _legacy_vaults() -> list[Path]:
    """Folders the 0.2 version wrote (vault/<source>/compendiums), removed with --vaults."""
    root = Path(os.getenv("ALCHEMY_VAULT_PATH", "").strip() or "vault").expanduser()
    if not root.is_dir():
        return []
    return [
        d
        for d in sorted(root.glob("*/compendiums"))
        if (d / "_INDEX.md").is_file() or (d / "INDEX.md").is_file()
    ]


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
        "  notebook-alchemy clean --labels         forget the AI's labels and sorting (re-runs cost again)\n"
        "  notebook-alchemy clean --records        forget past uploads and upload confirmations\n"
        "  notebook-alchemy clean --all --yes      all of the above, without asking\n"
    ),
)
@click.option("--all", "clean_all", is_flag=True, help="Captions, labels, records and old document folders.")
@click.option("--cache", "clean_cache", is_flag=True, help="Clear downloaded captions.")
@click.option("--labels", "clean_labels", is_flag=True, help="Forget the AI's labels and sorting.")
@click.option(
    "--records", "clean_records", is_flag=True, help="Forget past uploads and upload confirmations."
)
@click.option(
    "--vaults", "clean_vaults", is_flag=True, help="Delete document folders written by version 0.2."
)
@click.option("-y", "--yes", is_flag=True, help="Don't ask before deleting.")
@verbose_option
@handle_errors
def clean_pipeline(
    clean_all: bool, clean_cache: bool, clean_labels: bool, clean_records: bool, clean_vaults: bool, yes: bool
) -> None:
    """Remove downloaded captions, the AI's cached work, or this tool's upload records."""
    config = _config()
    engine = AlchemyEngine(config)
    do_labels = clean_all or clean_labels
    do_records = clean_all or clean_records
    do_vaults = clean_all or clean_vaults
    do_cache = clean_all or clean_cache or not (do_labels or do_records or do_vaults)

    targets: list[tuple[str, Path]] = []
    skipped: list[str] = []
    custom_cache = config.cache_dir != AlchemyConfig().cache_dir
    if do_cache and config.cache_dir.is_dir():
        # Only this tool's own cache records, directly in the folder: never other files or subfolders.
        for f in sorted(config.cache_dir.glob("*.json")):
            (targets.append(("cache", f)) if _is_cache_record(f) else skipped.append(f.name))
    if do_labels:
        for f in sorted(engine.knowledge_dir().glob("*.json")) if engine.knowledge_dir().is_dir() else []:
            (targets.append(("labels", f)) if _is_label_record(f) else skipped.append(str(f)))
        library = engine.home / "library"
        for f in sorted(library.glob("*.json")) if library.is_dir() else []:
            (targets.append(("labels", f)) if isinstance(_json(f), dict) else skipped.append(str(f)))
        if (engine.home / "batch.json").is_file():  # a batch still running at Google is forgotten too
            targets.append(("labels", engine.home / "batch.json"))
    if do_records:
        records_dir = engine.sync_records_dir()
        for f in sorted(records_dir.glob("*.json")) if records_dir.is_dir() else []:
            (targets.append(("record", f)) if _is_sync_record(f) else skipped.append(str(f)))
        for f in (consent_file(), engine.notebooks_path()):
            if f.is_file():
                targets.append(("record", f))
    if do_vaults:
        targets += [("vault", d) for d in _legacy_vaults()]
    for item in skipped:
        ui.warn(f"Skipped {esc(item)}: it does not look like Notebook Alchemy output.")
    if not targets:
        ui.success("Nothing to clean.")
        return

    kinds = {kind for kind, _p in targets}
    ask = (kinds & {"labels", "record", "vault"} or ("cache" in kinds and custom_cache)) and not yes
    if ask:
        count = {k: sum(1 for kind, _p in targets if kind == k) for k in kinds}
        if "cache" in kinds and custom_cache:
            ui.console.print(
                f"{_plural(count['cache'], 'caption file')} in a custom cache folder "
                f"{ui.name(config.cache_dir)} will be deleted."
            )
        if "labels" in kinds:
            ui.console.print(
                f"{_plural(count['labels'], 'file')} of the AI's labels and sorting will be deleted; "
                "the next run pays for them again."
            )
        if "record" in kinds:
            ui.console.print(
                f"{count['record']} local records will be deleted. Later runs will not remove sources "
                "that earlier "
                "runs uploaded, and the next upload asks you to confirm the account again."
            )
        if "vault" in kinds:
            ui.console.print("These document folders from version 0.2 will be deleted:")
            for kind, p in targets:
                if kind == "vault":
                    ui.console.print(f"  {esc(p)}")
        if not _ask("Do you want to continue"):
            ui.info("Cancelled. Nothing was deleted.")
            return

    freed, failed = 0, []
    for _kind, path in targets:
        try:
            freed += _remove(path)
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    for kind, p in targets:
        if kind == "vault":
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
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    before = sorted(p.name for p in Path.cwd().iterdir())
    try:
        config = AlchemyConfig.from_env()
        record("Configuration loads", True, f"environment: {config.env}")
    except AlchemyError as exc:
        record("Configuration loads", False, exc.message)

    with tempfile.TemporaryDirectory(prefix="notebook-alchemy-test-") as tmp:
        tmp_path = Path(tmp)
        engine = AlchemyEngine(AlchemyConfig(env="test", cache_dir=tmp_path / "cache", gemini_api_key=""))
        try:
            report = engine.build("sample", mock=True)
            record(
                "Sample videos read",
                len(report.read) == len(report.videos),
                f"{len(report.read)} of {len(report.videos)} videos",
            )
            text = " ".join(b.content for b in report.books)
            boiler = "subscribe" in text.lower() or "welcome back" in text.lower()
            record(
                "Repeated intros and calls to action removed",
                not boiler,
                "none left in the books" if not boiler else "found in the books",
            )
            topics = [b for b in report.books if b.title != OVERVIEW_TITLE]
            record(
                "Topic books are written",
                len(topics) >= 2,
                f"{len(topics)} topic books plus {OVERVIEW_TITLE}",
            )
            clean = all(not b.content.startswith("---") for b in report.books)
            record("Books are plain Markdown", clean, "no YAML header" if clean else "YAML header found")
            saved = engine.save_books(report.books, tmp_path / "books")
            record("Books can be saved", len(saved) == len(report.books), f"{len(saved)} files")
        except AlchemyError as exc:
            record("The sample builds", False, exc.message)
    untouched = sorted(p.name for p in Path.cwd().iterdir()) == before
    record(
        "Nothing written outside the temp folder",
        untouched,
        "working directory untouched" if untouched else "files were added here",
    )

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
