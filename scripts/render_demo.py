"""Render real notebook-alchemy output to an SVG, using Rich's own export.

The command runs for real (network, Gemini and NotebookLM included when you pass such a command);
this script only records what the terminal would have shown. Progress bars are replaced by the final
state, because an SVG cannot animate them.

Usage: python scripts/render_demo.py OUT.svg "window title" -- dry-run @Hyperplexed
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from rich.console import Console

from notebook_alchemy import cli, ui


class _NoProgress:
    """Stands in for ui.Reporter: no live bars in a recording."""

    def __enter__(self) -> _NoProgress:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __call__(self, stage: str, done: int, total: int) -> None:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("title")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="after --, the notebook-alchemy arguments")
    args = parser.parse_args()
    argv = [a for a in args.command if a != "--"]
    if not argv:
        parser.error("give the notebook-alchemy arguments after --")

    recorder = Console(record=True, force_terminal=True, width=100, file=io.StringIO(), highlight=False)
    ui.console = recorder
    ui.err_console = recorder
    ui.Reporter = _NoProgress  # type: ignore[misc,assignment]

    code = 0
    try:
        cli.cli.main(args=argv, standalone_mode=False)
    except SystemExit as exc:
        code = int(exc.code or 0)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    recorder.save_svg(str(args.out), title=args.title)
    print(f"wrote {args.out} (command exit {code})", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
