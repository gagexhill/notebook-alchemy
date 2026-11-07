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

from .config import DEFAULT_NOTEBOOKLM_PROFILE, AlchemyConfig
from .errors import ConfigError, NotebookLMError, SessionError

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


@dataclass
class AccountIdentity:
    """The Google account a notebooklm profile is signed in to, as the profile itself records it.

    `email` is None when the account cannot be read; it is never guessed. `detail` says why.
    """

    profile: str
    email: str | None
    detail: str = ""
    storage: str = ""  # set when ALCHEMY_SESSION_FILE, not the profile, holds the session

    @property
    def display(self) -> str:
        return self.email or "unknown"


def consent_file() -> Path:
    return Path.home() / ".notebook-alchemy" / "upload_accounts.json"


def _read_consents() -> dict[str, Any]:
    path = consent_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # An unreadable record means we cannot prove consent, so the user is asked again.
        logger.warning("Could not read %s (%s); upload confirmation will be asked again.", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def upload_confirmed(identity: AccountIdentity) -> bool:
    """True when uploads to this profile were confirmed for this same, known account.

    An unknown account is never confirmed: two different unknown accounts cannot be told apart.
    """
    if identity.email is None:
        return False
    entry = _read_consents().get(identity.profile)
    return isinstance(entry, dict) and "email" in entry and entry["email"] == identity.email


def record_upload_consent(identity: AccountIdentity) -> None:
    """Remember that the user confirmed uploads to this profile and account (a known one only)."""
    if identity.email is None:
        return
    path = consent_file()
    data = _read_consents()
    data[identity.profile] = {"email": identity.email}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise NotebookLMError(
            f"Could not record the upload confirmation in {path}: {exc}",
            hint="Check that the folder is writable, or pass --yes to skip the confirmation.",
        ) from exc


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
        profile: str | None = None,
        *,
        runner: Runner | None = None,
    ) -> None:
        self.config = config
        self.profile = profile or config.notebooklm_profile
        self._runner = runner

    # -- session --

    def profile_state_path(self) -> Path:
        return Path.home() / ".notebooklm" / "profiles" / self.profile / "storage_state.json"

    def storage_override(self) -> Path | None:
        """The ALCHEMY_SESSION_FILE in use, if it exists. notebooklm then ignores --profile."""
        path = self.config.session_file
        return path if path and path.is_file() else None

    def session_file(self) -> Path | None:
        """The session file notebooklm will use: ALCHEMY_SESSION_FILE if it exists, else the profile's."""
        override = self.storage_override()
        if override:
            return override
        state = self.profile_state_path()
        return state if state.is_file() else None

    def login_command(self, *extra: str) -> list[str]:
        """`notebooklm login` for the same session file every other call uses."""
        return self._command(["login", *extra], as_json=False)

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

    def upload_account(self) -> AccountIdentity:
        """Read the account recorded on this profile with `notebooklm auth check --json`.

        This is a local read (no network). The email is reported only when notebooklm itself
        recorded one; otherwise it is None with the reason in `detail`.
        """
        if os.getenv("NOTEBOOKLM_AUTH_JSON"):
            return AccountIdentity(
                self.profile, None, "NOTEBOOKLM_AUTH_JSON is set; its account is not recorded."
            )
        cmd = self._command(["auth", "check"], as_json=True)
        try:
            res = (self._runner or subprocess.run)(
                cmd, check=False, capture_output=True, text=True, encoding="utf-8", timeout=30.0
            )
        except FileNotFoundError:
            return AccountIdentity(self.profile, None, "the `notebooklm` command was not found")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return AccountIdentity(self.profile, None, f"`notebooklm auth check` could not run: {exc}")
        # auth check exits non-zero when a check fails but still prints its JSON report.
        try:
            data = json.loads((res.stdout or "").strip())
        except ValueError:
            return AccountIdentity(
                self.profile, None, f"`notebooklm auth check` returned no report (exit {res.returncode})"
            )
        account = data.get("account") if isinstance(data, dict) else None
        email = account.get("email") if isinstance(account, dict) else None
        if isinstance(email, str) and "@" in email.strip():
            return AccountIdentity(self.profile, email.strip(), storage=str(self.storage_override() or ""))
        storage = str(self.storage_override() or "")
        checks = data.get("checks") if isinstance(data, dict) else None
        if isinstance(checks, dict) and checks.get("storage_exists") is False:
            return AccountIdentity(self.profile, None, "no session is stored for this profile", storage)
        return AccountIdentity(
            self.profile, None, "this profile's sign-in did not record an account", storage
        )

    def browser_accounts(self, browser: str) -> list[str]:
        """Emails of the Google accounts a browser has signed in (`notebooklm auth inspect`)."""
        data = self._run(["auth", "inspect", "--browser", browser], timeout=LIST_TIMEOUT)
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, list):
            raise NotebookLMError("`notebooklm auth inspect` returned an unexpected shape.")
        return [str(a["email"]) for a in accounts if isinstance(a, dict) and a.get("email")]

    # -- process plumbing --

    def _command(self, args: list[str], *, as_json: bool) -> list[str]:
        cmd = [NOTEBOOKLM_BIN]
        # A configured path that does not exist (for example one copied from an old .env) is
        # ignored, so the notebooklm profile's own session is used.
        override = self.storage_override()
        if override:
            if self.profile != DEFAULT_NOTEBOOKLM_PROFILE:
                raise ConfigError(
                    f"ALCHEMY_SESSION_FILE ({override}) is set, so notebooklm would ignore "
                    f"profile '{self.profile}' and use that file's account instead.",
                    hint="Remove ALCHEMY_SESSION_FILE from .env to use profiles, or drop --profile "
                    "and ALCHEMY_NOTEBOOKLM_PROFILE to use that file.",
                )
            cmd += ["--storage", str(override)]
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
