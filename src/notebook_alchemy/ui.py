"""Terminal presentation: consistent steps, progress, errors, and safe text output.

The default output says what happened in everyday words; paths, token counts, models and other
detail print only with -v, the way gcloud keeps them behind --verbosity=info. Warnings and errors
go to stderr with a "Warning:" or "Error:" prefix, and a next step is written as a command to run.
"""

from __future__ import annotations

import datetime as dt
import sys
import traceback
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

from .errors import AlchemyError, SyncError

F = TypeVar("F", bound=Callable[..., Any])

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

EXIT_INTERRUPTED = 130
EXIT_UNEXPECTED = 70


class UiState:
    quiet = False
    debug = False
    verbose = 0


def esc(value: object) -> str:
    """Escape text from outside this program (titles, errors, paths) for Rich markup."""
    return escape(str(value))


def name(value: object) -> str:
    """A resource name in square brackets, as gcloud prints them: `[Kizzy Parks]`. Markup-safe."""
    return esc(f"[{value}]")


def configure(*, quiet: bool = False, no_color: bool = False, debug: bool = False, verbose: int = 0) -> None:
    global console, err_console
    UiState.quiet, UiState.debug, UiState.verbose = quiet, debug, verbose
    if no_color:
        console = Console(highlight=False, no_color=True)
        err_console = Console(stderr=True, highlight=False, no_color=True)


def info(message: str) -> None:
    if not UiState.quiet:
        console.print(message)


def detail(message: str) -> None:
    """Shown only with -v: paths, counts, models and other things most people never need."""
    if UiState.verbose and not UiState.quiet:
        console.print(f"[dim]{message}[/dim]")


def step(number: int, total: int, label: str) -> None:
    """A numbered step heading, e.g. `Step 2 of 3: Writing documents`."""
    if not UiState.quiet:
        console.print(f"\n[bold]Step {number} of {total}:[/bold] {esc(label)}")


def success(message: str) -> None:
    """A result. Printed even with -q, which keeps errors and final results."""
    console.print(f"[green]✓[/green] {message}")


def warn(message: str, *, always: bool = False) -> None:
    """A warning on stderr; `always` for ones -q must not hide (sample data, a missing account)."""
    if always or not UiState.quiet:
        err_console.print(f"[yellow]Warning:[/yellow] {message}")


def next_command(purpose: str, command: str) -> None:
    """A next step written as a command to run, the way gcloud phrases them."""
    info(f"\nTo {purpose}, run:\n  [bold]$ {esc(command)}[/bold]")


def print_error(exc: AlchemyError) -> None:
    """What failed, why, and the next action."""
    lines = [f"[bold red]Error:[/bold red] {esc(exc.message)}"]
    if isinstance(exc, SyncError):
        lines += [f"  - {esc(item)}" for item in exc.failed[:10]]
        if len(exc.failed) > 10:
            lines.append(f"  ... and {len(exc.failed) - 10} more")
    if exc.hint:
        lines.append(f"[bold]Next:[/bold] {esc(exc.hint)}")
    err_console.print("\n".join(lines))


def handle_errors(func: F) -> F:
    """Turn typed errors into a readable panel and a stable exit code; never a raw traceback."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except AlchemyError as exc:
            print_error(exc)
            if UiState.debug:
                err_console.print(esc(traceback.format_exc()))
            sys.exit(exc.exit_code)
        except KeyboardInterrupt:
            err_console.print(
                "\n[yellow]Stopped.[/yellow] Files on disk are complete. If an upload was running, run "
                "the same command again to finish it."
            )
            sys.exit(EXIT_INTERRUPTED)
        except (
            SystemExit,
            EOFError,
            click.ClickException,
            click.exceptions.Abort,
            click.exceptions.Exit,
        ):
            raise
        except Exception as exc:  # noqa: BLE001 - last-resort boundary for the CLI
            if UiState.debug:
                err_console.print(esc(traceback.format_exc()))
            err_console.print(
                f"[bold red]Error:[/bold red] Something unexpected went wrong ({esc(type(exc).__name__)}: "
                f"{esc(exc)}).\n[bold]Next:[/bold] Run the command again with --debug and include its "
                "output in a bug report."
            )
            sys.exit(EXIT_UNEXPECTED)

    return wrapper  # type: ignore[return-value]


class Reporter:
    """Progress callback `(stage, done, total)`: bars on a terminal, one line per stage otherwise."""

    def __init__(self) -> None:
        self._progress: Progress | None = None
        self._tasks: dict[str, TaskID] = {}
        self._seen: set[str] = set()

    def __enter__(self) -> Reporter:
        if console.is_terminal and not UiState.quiet:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(bar_width=28),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            )
            self._progress.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._progress:
            self._progress.stop()

    def __call__(self, stage: str, done: int, total: int) -> None:
        if UiState.quiet:
            return
        if self._progress is not None:
            if stage not in self._tasks:
                self._tasks[stage] = self._progress.add_task(esc(stage), total=max(total, 1))
            self._progress.update(self._tasks[stage], completed=done, total=max(total, 1))
            return
        if done >= total and stage not in self._seen:  # not a terminal: one line per finished stage
            self._seen.add(stage)
            detail(f"  {esc(stage)}: {done}/{total}")


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def ago(iso: str, now: dt.datetime | None = None) -> str:
    """An ISO time as "5 minutes ago", "3 hours ago" or "2 days ago"; the text itself if unreadable."""
    try:
        then = dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso or "at an unknown time"
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.UTC)
    seconds = max(0, int(((now or dt.datetime.now(dt.UTC)) - then).total_seconds()))
    for size, unit in ((86_400, "day"), (3_600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"
