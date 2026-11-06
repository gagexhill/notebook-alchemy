"""NotebookLM access through the `notebooklm` CLI (notebooklm-py).

Every call checks the process result. A failure raises a typed error; nothing here reports
success for work that did not happen.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AlchemyConfig
from .errors import NotebookLMError, SessionError

logger = logging.getLogger(__name__)

NOTEBOOKLM_BIN = "notebooklm"
LIST_TIMEOUT = 60.0
WRITE_TIMEOUT = 120.0
UPLOAD_TIMEOUT = 300.0
_AUTH_HINTS = ("authentication", "auth not found", "sign-in", "signed-out", "login", "re-authenticate")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]  # defaults to subprocess.run at call time


@dataclass
class SourceDocument:
    """A document to upload as a NotebookLM source."""

    source_id: str
    title: str
    content: str
    source_type: str = "document"


@dataclass
class SessionStatus:
    ok: bool
    detail: str
    notebooks: int = 0


def normalize_title(title: str) -> str:
    """Compare source titles the way NotebookLM may rewrite them: no `.md`, underscores as spaces."""
    stem = title.strip()
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    return " ".join(stem.replace("_", " ").split()).lower()


def _first(data: Any, *keys: str) -> Any:
    if isinstance(data, dict):
        for key in keys:
            if data.get(key):
                return data[key]
    return None


class NotebookLMClient:
    """Notebook management and source upload through the notebooklm CLI."""

    def __init__(
        self,
        config: AlchemyConfig,
        profile: str = "default",
        *,
        runner: Runner | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self._runner = runner

    # -- session --

    def profile_state_path(self) -> Path:
        return Path.home() / ".notebooklm" / "profiles" / self.profile / "storage_state.json"

    def session_file(self) -> Path | None:
        """First existing session file: explicit file, state dir, then the notebooklm profile."""
        candidates: list[Path] = []
        if self.config.session_file:
            candidates.append(self.config.session_file)
        candidates.append(self.config.state_dir / "storage_state.json")
        candidates.append(self.profile_state_path())
        return next((p for p in candidates if p.is_file()), None)

    def is_authenticated(self) -> bool:
        """True when session material exists. Use check_session() to know it still works."""
        return bool(os.getenv("NOTEBOOKLM_AUTH_JSON")) or self.session_file() is not None

    def cli_version(self) -> str | None:
        """Version text from `notebooklm --version`, or None when the CLI cannot run."""
        try:
            res = (self._runner or subprocess.run)(
                [NOTEBOOKLM_BIN, "--version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (res.stdout or "").strip() or None if res.returncode == 0 else None

    def check_session(self) -> SessionStatus:
        """Ask NotebookLM whether the stored session works (one read-only list call)."""
        if not self.is_authenticated():
            return SessionStatus(False, "No NotebookLM session found.")
        try:
            notebooks = self.list_notebooks()
        except NotebookLMError as exc:
            return SessionStatus(False, exc.message)
        return SessionStatus(True, "Session is active.", len(notebooks))

    # -- process plumbing --

    def _command(self, args: list[str], *, as_json: bool) -> list[str]:
        cmd = [NOTEBOOKLM_BIN]
        # A configured path that does not exist (for example one copied from an old .env) is
        # ignored, so the notebooklm profile's own session is used.
        if self.config.session_file and self.config.session_file.is_file():
            cmd += ["--storage", str(self.config.session_file)]
        cmd += ["--profile", self.profile, *args]
        if as_json:
            cmd.append("--json")
        return cmd

    def _run(self, args: list[str], *, timeout: float, as_json: bool = True) -> Any:
        cmd = self._command(args, as_json=as_json)
        shown = "notebooklm " + " ".join(args[:2] if args[0] == "source" else args[:1])
        try:
            res = (self._runner or subprocess.run)(
                cmd, check=False, capture_output=True, text=True, encoding="utf-8", timeout=timeout
            )
        except FileNotFoundError as exc:
            raise NotebookLMError(
                "The `notebooklm` command was not found.",
                hint="Install it with: pip install notebooklm-py",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise NotebookLMError(
                f"`{shown}` did not finish within {timeout:.0f}s.",
                hint="Check your connection and try again.",
            ) from exc
        except OSError as exc:
            raise NotebookLMError(f"Could not start `{shown}`: {exc}") from exc

        out = (res.stdout or "").strip()
        if res.returncode != 0:
            message = ""
            try:
                parsed = json.loads(out or (res.stderr or ""))
                message = str(parsed.get("message", "")) if isinstance(parsed, dict) else ""
            except ValueError:
                pass
            message = message or (res.stderr or "").strip() or out or f"exit code {res.returncode}"
            message = " ".join(message.split())[:400]
            message = re.sub(r"\s*Run 'notebooklm login'[^.]*\.", "", message).strip()
            if any(h in message.lower() for h in _AUTH_HINTS):
                raise SessionError(
                    f"NotebookLM rejected the session: {message}",
                    hint="Run `notebook-alchemy login`.",
                )
            raise NotebookLMError(f"`{shown}` failed: {message}")
        if not as_json:
            return out
        try:
            return json.loads(out)
        except ValueError as exc:
            raise NotebookLMError(f"`{shown}` returned output that is not JSON: {out[:120]!r}") from exc

    # -- notebooks --

    def list_notebooks(self) -> list[dict[str, Any]]:
        data = self._run(["list"], timeout=LIST_TIMEOUT)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("notebooks"), list):
            return data["notebooks"]
        raise NotebookLMError("`notebooklm list` returned an unexpected shape.")

    def find_notebook(self, query: str, *, exact_only: bool = False) -> dict[str, Any] | None:
        """Exact title or id match; otherwise a unique substring match. Ambiguity is an error."""
        q = query.strip().lower()
        notebooks = self.list_notebooks()
        for nb in notebooks:
            if q == str(nb.get("id", "")).lower() or q == str(nb.get("title", "")).lower():
                return nb
        if exact_only:
            return None
        partial = [nb for nb in notebooks if q and q in str(nb.get("title", "")).lower()]
        if len(partial) > 1:
            names = ", ".join(repr(nb.get("title", "?")) for nb in partial[:5])
            raise NotebookLMError(
                f"'{query}' matches {len(partial)} notebooks: {names}.",
                hint="Use the full title or the notebook id.",
            )
        return partial[0] if partial else None

    def create_notebook(self, title: str) -> tuple[str, bool]:
        """Return (notebook id, created). Reuses an existing notebook with exactly this title."""
        existing = self.find_notebook(title, exact_only=True)
        if existing and existing.get("id"):
            return str(existing["id"]), False
        data = self._run(["create", title], timeout=WRITE_TIMEOUT)
        nb_id = _first(data, "id", "notebook_id") or _first(_first(data, "notebook"), "id")
        if not nb_id:
            raise NotebookLMError(f"NotebookLM created '{title}' but returned no notebook id.")
        return str(nb_id), True

    # -- sources --

    def list_sources(self, notebook_id: str) -> list[dict[str, Any]]:
        data = self._run(["source", "list", "-n", notebook_id], timeout=LIST_TIMEOUT)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("sources"), list):
            return data["sources"]
        raise NotebookLMError("`notebooklm source list` returned an unexpected shape.")

    def delete_source(self, notebook_id: str, source_id: str) -> None:
        self._run(["source", "delete", source_id, "-n", notebook_id, "-y"], timeout=WRITE_TIMEOUT)

    def wait_for_source(self, notebook_id: str, source_id: str, timeout: int = 180) -> None:
        """Block until NotebookLM has finished processing a source; raise if it failed or timed out."""
        self._run(
            ["source", "wait", source_id, "-n", notebook_id, "--timeout", str(timeout)],
            timeout=timeout + 30,
            as_json=False,
        )

    def rename_source(self, notebook_id: str, source_id: str, title: str) -> None:
        self._run(["source", "rename", source_id, title, "-n", notebook_id], timeout=WRITE_TIMEOUT)

    def add_source_document(self, notebook_id: str, document: SourceDocument) -> str:
        """Upload one document as a file source (content never travels on the command line)."""
        safe = re.sub(r"[^\w\-]+", "_", document.title).strip("_") or "source"
        with tempfile.TemporaryDirectory(prefix="notebook-alchemy-") as tmp:
            path = Path(tmp) / f"{safe[:80]}.md"
            path.write_text(document.content, encoding="utf-8")
            data = self._run(
                [
                    "source",
                    "add",
                    str(path),
                    "-n",
                    notebook_id,
                    "--type",
                    "file",
                    "--title",
                    document.title,
                ],
                timeout=UPLOAD_TIMEOUT,
            )
        source_id = _first(data, "id", "source_id") or _first(_first(data, "source"), "id")
        return str(source_id or document.source_id)
