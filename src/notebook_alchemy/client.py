"""NotebookLM access through notebooklm-py's Python API.

Notebooks and sources are read and changed through the library, on one session per client that
stays open for the run. The `notebooklm` command is used only where the library has no public
Python function: the sign-in window (`login`) and reading the account a profile recorded
(`auth check`). A failure raises a typed error; nothing here reports success for work that did
not happen.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import hmac
import importlib.metadata
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_NOTEBOOKLM_PROFILE, AlchemyConfig
from .errors import ConfigError, NotebookLMError, SessionError

logger = logging.getLogger(__name__)

NOTEBOOKLM_BIN = "notebooklm"


LIST_TIMEOUT = 60.0
OPEN_TIMEOUT = 60.0  # opening the session: reading the sign-in file and one request for its tokens
CLOSE_TIMEOUT = 10.0
# After a call runs out of time: how long it is given to let go of what it was doing.
SETTLE_TIMEOUT = 30.0
# For answers from NotebookLM this tool cannot read: usually a newer or older notebooklm-py.
UNEXPECTED_HINT = (
    "Run the same command again. If it keeps failing, update Notebook Alchemy by running its "
    "installer again, then check with `notebook-alchemy test`."
)
# The notebooklm tool comes with Notebook Alchemy, so a missing one is a broken install.
INSTALL_HINT = (
    "Run the Notebook Alchemy installer again; it installs the notebooklm tool too. "
    "In a source checkout, run: pip install -e ."
)
WRITE_TIMEOUT = 120.0
UPLOAD_TIMEOUT = 300.0
# notebooklm's own HTTP timeout for an upload (its default is short for a large book); kept below
# UPLOAD_TIMEOUT so notebooklm reports the timeout itself.
UPLOAD_HTTP_TIMEOUT = 180.0
RETRY_HINT = "Run the same command again; `notebook-alchemy status` shows whether the sign-in works."

Runner = Callable[..., "subprocess.CompletedProcess[str]"]  # defaults to subprocess.run at call time
# Opens notebooklm-py for a profile or a session file: (profile, path) -> an async context manager
# whose value has `.notebooks`, `.sources` and `.settings`.
ApiOpener = Callable[[str | None, str | None], Any]


def default_api(profile: str | None, path: str | None) -> Any:
    """notebooklm-py's client for a profile, or for a session file when one is given."""
    from notebooklm import NotebookLMClient as _Library  # imported on use: it takes half a second
    from notebooklm.options import ClientConfig, TimeoutOptions, TransferOptions

    # The library's usual wait for an answer is short for a large book, so both steps of an upload
    # get a longer one. allow_headless stays off: renewing an ended sign-in through a hidden browser
    # made a signed-out check take minutes; an ended sign-in is offered to the user instead.
    window = TimeoutOptions(connect=10.0, read=UPLOAD_HTTP_TIMEOUT, write=UPLOAD_HTTP_TIMEOUT, pool=10.0)
    config = ClientConfig(transfers=TransferOptions(start_timeout=window, finalize_timeout=window))
    return _Library.from_storage(path=path, profile=profile, allow_headless=False, config=config)


class _NoSignIn(Exception):
    """The sign-in file is missing or cannot be used."""


class _OpenFailed(Exception):
    """The session could not be opened, for a reason signing in again would not fix."""


class _CallTimeout(Exception):
    """A call did not finish in the time this tool allows."""


class _Closed(Exception):
    """The session was closed while the call was waiting."""


_OPEN: set[_Session] = set()
_OPEN_LOCK = threading.Lock()


def close_all() -> None:
    """Close every open NotebookLM session. Runs at exit; safe to call at any time."""
    with _OPEN_LOCK:
        sessions = list(_OPEN)
    for session in sessions:
        session.close()


atexit.register(close_all)  # a backstop; a command closes its sessions when it ends


class _Pending:
    """One call handed to the session's event loop, and how it ended."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.task: asyncio.Task[Any] | None = None
        self.value: Any = None
        self.error: BaseException | None = None

    def finish(self, value: Any = None, error: BaseException | None = None) -> None:
        if not self.done.is_set():
            self.value, self.error = value, error
            self.done.set()


def _submit(loop: asyncio.AbstractEventLoop, make: Callable[[], Awaitable[Any]]) -> _Pending:
    """Start `make()` on the loop. The result is recorded when the task itself ends, so a cancelled
    call is known to have let go only once it really has."""
    pending = _Pending()

    def ended(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            pending.finish(error=asyncio.CancelledError())
        elif task.exception() is not None:
            pending.finish(error=task.exception())
        else:
            pending.finish(value=task.result())

    def start() -> None:
        try:
            pending.task = loop.create_task(make())  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - handed to the thread that is waiting
            pending.finish(error=exc)
            return
        pending.task.add_done_callback(ended)

    loop.call_soon_threadsafe(start)
    return pending


def _cancel(loop: asyncio.AbstractEventLoop, pending: _Pending) -> None:
    def stop() -> None:
        if pending.task is not None:
            pending.task.cancel()

    try:
        loop.call_soon_threadsafe(stop)
    except RuntimeError:  # the loop is already closed: nothing is running on it
        pending.finish(error=asyncio.CancelledError())


def _is_sign_in_file_problem(exc: BaseException) -> bool:
    """A missing sign-in file, one that is not JSON, or one without the cookies Google needs.

    The library raises other ValueErrors while opening (a page it cannot read, a region block)
    that signing in again would not fix; those are not sign-in problems. The cookie error is
    matched by name because the library keeps that class in a private module.
    """
    if isinstance(exc, (FileNotFoundError, json.JSONDecodeError)):
        return True
    return any(kind.__name__ == "RequiredCookieValidationError" for kind in type(exc).__mro__)


class _Session:
    """One notebooklm-py client on one background event loop.

    The library is asynchronous and its client belongs to the event loop that opened it, while
    uploads run on several threads. Every call is handed to that one loop and waited on, so all
    threads share one client and one connection pool. Opened on first use; once closed it stays
    closed, and the client makes a new one.
    """

    def __init__(self, opener: Callable[[], Any]) -> None:
        self._opener = opener
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._context: Any = None
        self._library: Any = None
        self._closed = False
        self._waiting: set[_Pending] = set()
        self._waiting_lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._library is not None

    @property
    def is_closed(self) -> bool:
        return self._closed

    def run(self, call: Callable[[Any], Awaitable[Any]], timeout: float) -> Any:
        loop, library = self._open()
        try:
            pending = _submit(loop, lambda: call(library))
        except RuntimeError as exc:  # closed between opening and starting
            raise _Closed() from exc
        with self._waiting_lock:
            self._waiting.add(pending)
        try:
            if pending.done.wait(timeout):
                if isinstance(pending.error, asyncio.CancelledError):
                    raise _Closed() from pending.error
                if pending.error is not None:
                    raise pending.error
                return pending.value
            # Out of time. The library finishes a request NotebookLM already has before it lets
            # go, so wait for the call to end: the caller's file is then no longer in use, and an
            # answer that arrived in the meantime is used, not thrown away.
            _cancel(loop, pending)
            pending.done.wait(SETTLE_TIMEOUT)
            if pending.done.is_set() and pending.error is None:
                return pending.value
            raise _CallTimeout()
        finally:
            with self._waiting_lock:
                self._waiting.discard(pending)

    def _open(self) -> tuple[asyncio.AbstractEventLoop, Any]:
        with self._lock:
            if self._closed:
                raise _Closed()
            if self._loop is not None and self._library is not None:
                return self._loop, self._library
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="notebooklm-session", daemon=True)
            thread.start()

            async def enter() -> tuple[Any, Any]:
                context = self._opener()
                return context, await context.__aenter__()

            pending = _submit(loop, enter)
            try:
                finished = pending.done.wait(OPEN_TIMEOUT)
            except BaseException:  # Ctrl+C while opening: let the open unwind, then stop the loop
                _cancel(loop, pending)
                pending.done.wait(CLOSE_TIMEOUT)
                _stop_loop(loop, thread)
                raise
            if not finished:
                _cancel(loop, pending)
                pending.done.wait(CLOSE_TIMEOUT)
            if not finished or pending.error is not None:
                _stop_loop(loop, thread)
                error = pending.error
                if not finished or error is None:
                    raise _CallTimeout()
                if _is_sign_in_file_problem(error):
                    raise _NoSignIn(
                        "missing" if isinstance(error, FileNotFoundError) else "unusable"
                    ) from error
                if isinstance(error, ValueError):
                    raise _OpenFailed(str(error)) from error
                raise error
            self._context, self._library = pending.value
            self._loop, self._thread = loop, thread
            with _OPEN_LOCK:
                _OPEN.add(self)
            return loop, self._library

    def close(self) -> None:
        with self._lock:
            loop, thread, context = self._loop, self._thread, self._context
            self._loop = self._thread = self._context = self._library = None
            self._closed = True
        with _OPEN_LOCK:
            _OPEN.discard(self)
        if loop is None or thread is None:
            return
        with self._waiting_lock:
            waiting = list(self._waiting)
        for pending in waiting:  # calls still under way are told to stop, and given time to
            _cancel(loop, pending)
        for pending in waiting:
            pending.done.wait(CLOSE_TIMEOUT)
        closing = _submit(loop, lambda: context.__aexit__(None, None, None))
        if not closing.done.wait(CLOSE_TIMEOUT):
            logger.info("Closing the NotebookLM session did not finish in %ss.", CLOSE_TIMEOUT)
        elif closing.error is not None:  # closing is cleanup; the work is done, the reason is logged
            logger.info("Closing the NotebookLM session: %s", type(closing.error).__name__)
        _stop_loop(loop, thread)
        for pending in waiting:  # none is left waiting on a loop that no longer runs
            pending.finish(error=asyncio.CancelledError())


def _stop_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(CLOSE_TIMEOUT)
    if not thread.is_alive():
        loop.close()


def _said(exc: BaseException) -> str:
    """What an error says, on one line, without notebooklm's own "run notebooklm login" advice."""
    text = " ".join(str(exc).split())[:400]
    return re.sub(r"\s*Run 'notebooklm login'[^.]*\.", "", text).strip() or type(exc).__name__


def _unreachable(exc: BaseException, what: str) -> NotebookLMError:
    return NotebookLMError(
        f"NotebookLM could not be reached ({what}): {_said(exc)}",
        hint="Check your connection and run the same command again.",
    )


def _limited(what: str, wait: object = None) -> NotebookLMError:
    asked = f" It asks for {wait} seconds." if wait else ""
    return NotebookLMError(
        f"NotebookLM is limiting requests from this account ({what}).{asked}",
        hint="Wait a few minutes, then run the same command again.",
    )


def _translate(exc: BaseException, what: str, timeout: float) -> NotebookLMError | None:
    """A failure as this tool's typed error, chosen by the error's type, never by its words.

    None for an error that is not NotebookLM's or the network's (a bug here or in a test fake),
    which is raised as it is.
    """
    if isinstance(exc, _NoSignIn):
        return SessionError(f"The saved NotebookLM sign-in is {exc}.", hint="Run `notebook-alchemy login`.")
    if isinstance(exc, _OpenFailed):
        return NotebookLMError(
            f"NotebookLM's sign-in could not be checked ({what}): {_said(exc)}", hint=UNEXPECTED_HINT
        )
    if isinstance(exc, _Closed):
        return NotebookLMError(
            f"The NotebookLM session was closed before the request finished ({what}).", hint=RETRY_HINT
        )
    if isinstance(exc, _CallTimeout):
        return NotebookLMError(
            f"NotebookLM did not finish in {timeout:.0f}s ({what}).",
            hint="Check your connection and run the same command again.",
        )
    import httpx  # the library's own HTTP client; both imported on use
    import notebooklm.exceptions as lib

    error: NotebookLMError | None = None
    if isinstance(exc, lib.AuthError):
        error = SessionError(
            f"NotebookLM rejected the session: {_said(exc)}", hint="Run `notebook-alchemy login`."
        )
    elif isinstance(exc, lib.RateLimitError):
        error = _limited(what, getattr(exc, "retry_after", None))
    elif isinstance(exc, lib.NotebookLimitError):
        error = NotebookLMError(
            "This NotebookLM account has as many notebooks as its plan allows.",
            hint="Delete a notebook you no longer need in NotebookLM, then run the same command again.",
        )
    elif isinstance(exc, lib.SourceTimeoutError):
        error = NotebookLMError(
            f"NotebookLM was still processing the source when the wait ended ({what}).",
            hint="Run the same command again; a source that is ready by then is not uploaded twice.",
        )
    elif isinstance(exc, lib.SourceProcessingError):
        error = NotebookLMError(f"NotebookLM could not process the source ({what}).", hint=RETRY_HINT)
    elif isinstance(exc, lib.SourceNotFoundError):
        error = NotebookLMError(f"The source is no longer in the notebook ({what}).", hint=RETRY_HINT)
    elif isinstance(exc, lib.NetworkError):  # includes the library's request timeout
        error = _unreachable(exc, what)
    elif isinstance(exc, lib.DecodingError):
        error = NotebookLMError(
            f"NotebookLM answered in a form this tool cannot read ({what}): {_said(exc)}",
            hint=UNEXPECTED_HINT,
        )
    elif isinstance(exc, lib.NotebookLMError):
        error = NotebookLMError(f"NotebookLM refused ({what}): {_said(exc)}", hint=RETRY_HINT)
    # Opening the session asks Google for its tokens with plain HTTP; those errors are not wrapped.
    elif isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        error = (
            _limited(what)
            if status == 429
            else NotebookLMError(f"NotebookLM answered HTTP {status} ({what}).", hint=RETRY_HINT)
        )
    elif isinstance(exc, (httpx.TransportError, TimeoutError)):
        error = _unreachable(exc, what)
    if error is not None:
        # A source NotebookLM registered before the failure: the caller records it, so the next
        # run can remove the half-made copy.
        error.source_id = str(getattr(exc, "source_id", "") or "") or None  # type: ignore[attr-defined]
    return error


@dataclass
class SourceDocument:
    """A document to upload as a NotebookLM source."""

    source_id: str
    title: str
    content: str


# NotebookLM's subscription tier ids are opaque enum keys, not a ranking (notebooklm-py
# `AccountLimits.tier`, per support.google.com/notebooklm/answer/16213268).
TIER_NAMES = {1: "Standard", 2: "Pro", 3: "Ultra", 4: "Plus", 5: "Expanded", 6: "Ultra"}


@dataclass(frozen=True)
class PlanLimits:
    """What the upload account's NotebookLM plan allows, as NotebookLM itself reports it."""

    source_limit: int | None
    notebook_limit: int | None
    tier: int | None

    @property
    def plan(self) -> str:
        return TIER_NAMES.get(self.tier or 0, "unknown plan")


LimitsReader = Callable[["NotebookLMClient"], PlanLimits | None]


@dataclass
class SessionStatus:
    ok: bool
    detail: str
    notebooks: int = 0
    signed_out: bool = False  # not ok because there is no working sign-in (not a network or tool fault)


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


def _account_hash(email: str, salt: str) -> str:
    """Salted SHA-256 of the email: enough to notice the account changed, without storing who it is."""
    return hashlib.sha256(f"{salt}:{email.strip().casefold()}".encode()).hexdigest()


def upload_confirmed(identity: AccountIdentity) -> bool:
    """True when uploads to this profile were confirmed for this same, known account.

    An unknown account is never confirmed: two different unknown accounts cannot be told apart. A
    record in any other shape (including the plain-email form of earlier versions) asks again.
    """
    if identity.email is None:
        return False
    data = _read_consents()
    salt = data.get("salt")
    entry = data.get("profiles", {}).get(identity.profile) if isinstance(data.get("profiles"), dict) else None
    if not isinstance(salt, str) or not isinstance(entry, str):
        return False
    return hmac.compare_digest(entry.encode(), _account_hash(identity.email, salt).encode())


def record_upload_consent(identity: AccountIdentity) -> None:
    """Remember that the user confirmed uploads to this profile and account (a known one only).

    The file holds a random per-install salt and, per profile, a salted hash of the account email;
    the email itself is never written.
    """
    if identity.email is None:
        return
    path = consent_file()
    data = _read_consents()
    salt = data.get("salt") if isinstance(data.get("salt"), str) else secrets.token_hex(16)
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    profiles[identity.profile] = _account_hash(identity.email, salt)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"salt": salt, "profiles": profiles}, indent=2), encoding="utf-8")
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


class NotebookLMClient:
    """Notebook management and source upload through notebooklm-py.

    `api` replaces the library in tests; `runner` replaces the `notebooklm` command, which is used
    for the sign-in window and for reading the recorded account only.
    """

    def __init__(
        self,
        config: AlchemyConfig,
        profile: str | None = None,
        *,
        runner: Runner | None = None,
        limits_reader: LimitsReader | None = None,
        api: ApiOpener | None = None,
    ) -> None:
        self.config = config
        self.profile = profile or config.notebooklm_profile
        self._runner = runner
        self._limits_reader = limits_reader
        self._api = api
        self._session: _Session | None = None
        self._session_lock = threading.Lock()

    # -- session --

    def profile_state_path(self) -> Path:
        """Where notebooklm keeps this profile's sign-in, by notebooklm's own rule: NOTEBOOKLM_HOME
        or `~/.notebooklm`, and for the default profile the older file in that folder's root when
        the profile has none. Worked out here because importing the library takes half a second
        and `status` asks this on every start."""
        configured = os.environ.get("NOTEBOOKLM_HOME")
        home = Path(configured).expanduser().resolve() if configured else Path.home() / ".notebooklm"
        state = home / "profiles" / self.profile / "storage_state.json"
        legacy = home / "storage_state.json"
        if self.profile == DEFAULT_NOTEBOOKLM_PROFILE and not state.exists() and legacy.exists():
            return legacy
        return state

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
        """The installed notebooklm-py version, or None when the `notebooklm` command is missing.

        Read from package metadata: starting `notebooklm --version` takes seconds.
        """
        if shutil.which(NOTEBOOKLM_BIN) is None:
            return None
        try:
            version = importlib.metadata.version("notebooklm-py")
        except importlib.metadata.PackageNotFoundError:
            return "on PATH, but not installed in this Python (version unknown)"
        return f"notebooklm-py {version}"

    def plan_limits(self) -> PlanLimits | None:
        """The account's source and notebook limits, read live from NotebookLM; None when unknown.

        One read-only request on the shared session.
        Unknown is reported as unknown: callers never substitute a guessed plan.
        """
        if self._limits_reader is not None:
            return self._limits_reader(self)
        if self._runner is not None or self._api is not None:
            return None  # an injected test double: no live requests from here
        if os.getenv("NOTEBOOKLM_AUTH_JSON") is None and self.session_file() is None:
            return None
        return self._read_plan_limits_live()

    def _read_plan_limits_live(self) -> PlanLimits | None:
        try:
            got = self._call("read the plan limits", lambda lib: lib.settings.get_account_limits())
        except Exception as exc:  # noqa: BLE001 - the limit is advisory; the reason is logged, not hidden
            logger.info("Could not read the NotebookLM plan limits: %s", type(exc).__name__)
            return None
        return PlanLimits(got.source_limit, got.notebook_limit, got.tier)

    def check_session(self) -> SessionStatus:
        """Ask NotebookLM whether the stored session works (one read-only list call)."""
        if not self.is_authenticated():
            return SessionStatus(False, "No NotebookLM session found.", signed_out=True)
        try:
            notebooks = self.list_notebooks()
        except SessionError as exc:
            return SessionStatus(False, exc.message, signed_out=True)
        except NotebookLMError as exc:
            return SessionStatus(False, exc.message)
        return SessionStatus(True, "Session is active.", len(notebooks))

    def upload_account(self) -> AccountIdentity:
        """Read the account recorded on this profile with `notebooklm auth check --json`.

        This is a local read (no network). The email is reported only when notebooklm itself
        recorded one; otherwise it is None with the reason in `detail`. It stays on the command
        line: the library's public Python API has no function for it.
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

    # -- the notebooklm command: the sign-in window and the recorded account only --

    def _refuse_profile_with_session_file(self) -> Path | None:
        """The session file in use, if any; refused when a profile was also chosen.

        A configured path that does not exist (for example one copied from an old .env) is
        ignored, so the notebooklm profile's own session is used.
        """
        override = self.storage_override()
        if override and self.profile != DEFAULT_NOTEBOOKLM_PROFILE:
            raise ConfigError(
                f"ALCHEMY_SESSION_FILE ({override}) is set, so notebooklm would ignore "
                f"profile '{self.profile}' and use that file's account instead.",
                hint="Remove ALCHEMY_SESSION_FILE from .env to use profiles, or drop --profile "
                "and ALCHEMY_NOTEBOOKLM_PROFILE to use that file.",
            )
        return override

    def _command(self, args: list[str], *, as_json: bool) -> list[str]:
        cmd = [NOTEBOOKLM_BIN]
        override = self._refuse_profile_with_session_file()
        if override:
            cmd += ["--storage", str(override)]
        cmd += ["--profile", self.profile, *args]
        if as_json:
            cmd.append("--json")
        return cmd

    # -- the library --

    def _open_session(self) -> _Session:
        with self._session_lock:
            # A session closed elsewhere (every session is closed when a command ends) is replaced.
            if self._session is None or self._session.is_closed:
                override = self._refuse_profile_with_session_file()
                profile, path = (None, str(override)) if override else (self.profile, None)
                opener = self._api or default_api
                self._session = _Session(lambda: opener(profile, path))
            return self._session

    def _call(self, what: str, call: Callable[[Any], Awaitable[Any]], *, timeout: float | None = None) -> Any:
        """Run one library call on the shared session; a failure becomes a typed error."""
        timeout = LIST_TIMEOUT if timeout is None else timeout
        session = self._open_session()
        try:
            return session.run(call, timeout)
        except Exception as exc:
            error = _translate(exc, what, timeout)
            if error is None:
                raise
            raise error from exc

    @property
    def session_is_open(self) -> bool:
        return self._session is not None and self._session.is_open

    def close(self) -> None:
        """Close the NotebookLM session, if one was opened. The next call opens a new one."""
        with self._session_lock:
            session, self._session = self._session, None
        if session is not None:
            session.close()

    # -- notebooks --

    def list_notebooks(self) -> list[dict[str, Any]]:
        notebooks = self._call("list the notebooks", lambda lib: lib.notebooks.list())
        return [{"id": str(n.id), "title": str(n.title or "")} for n in notebooks]

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
        made = self._call(
            "create the notebook", lambda lib: lib.notebooks.create(title), timeout=WRITE_TIMEOUT
        )
        nb_id = getattr(made, "id", None)
        if not nb_id:
            raise NotebookLMError(
                f"NotebookLM created '{title}' but returned no notebook id.",
                hint="Run the same command again: it finds the notebook by its title and uses it.",
            )
        return str(nb_id), True

    # -- sources --

    def list_sources(self, notebook_id: str) -> list[dict[str, Any]]:
        sources = self._call("list the sources", lambda lib: lib.sources.list(notebook_id))
        return [_source_record(s) for s in sources]

    def delete_source(self, notebook_id: str, source_id: str) -> None:
        self._call(
            "remove a source", lambda lib: lib.sources.delete(notebook_id, source_id), timeout=WRITE_TIMEOUT
        )

    def wait_for_source(self, notebook_id: str, source_id: str, timeout: int = 180) -> None:
        """Block until NotebookLM has finished processing a source; raise if it failed or timed out."""
        # The library does the waiting and reports the timeout; ours is only a later backstop.
        self._call(
            f"waited {timeout}s for a source",
            lambda lib: lib.sources.wait_until_ready(notebook_id, source_id, timeout=float(timeout)),
            timeout=timeout + 30,
        )

    def add_youtube_sources(self, notebook_id: str, urls: list[str]) -> None:
        """Queue YouTube videos as sources without waiting; NotebookLM fetches their transcripts."""
        self._call(
            "add videos",
            lambda lib: lib.sources.add_urls_async(notebook_id, list(urls)),
            timeout=WRITE_TIMEOUT,
        )

    def source_text(self, notebook_id: str, source_id: str) -> str:
        """The full text NotebookLM holds for a source (for a video, its transcript)."""
        got = self._call("read a source", lambda lib: lib.sources.get_fulltext(notebook_id, source_id))
        return str(getattr(got, "content", "") or "")

    def rename_source(self, notebook_id: str, source_id: str, title: str) -> None:
        self._call(
            "rename a source",
            lambda lib: lib.sources.rename(notebook_id, source_id, title),
            timeout=WRITE_TIMEOUT,
        )

    def add_source_document(self, notebook_id: str, document: SourceDocument) -> str:
        """Upload one document as a file source. The text goes as a file, as it always has: a book
        is far larger than one command-line argument may be, and the library uploads files."""
        safe = re.sub(r"[^\w\-]+", "_", document.title).strip("_") or "source"
        # A scanner briefly holding the temp file must not turn a finished upload into an error.
        with tempfile.TemporaryDirectory(prefix="notebook-alchemy-", ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / f"{safe[:80]}.md"
            path.write_text(document.content, encoding="utf-8")
            made = self._call(
                f"upload '{document.title}'",
                lambda lib: lib.sources.add_file(notebook_id, path, "text/markdown", title=document.title),
                timeout=UPLOAD_TIMEOUT,
            )
        source_id = getattr(made, "id", None)
        if not source_id:
            # Without the new source's id it cannot be waited on, renamed or told apart from an
            # older copy, so the upload is not reported as done.
            raise NotebookLMError(
                f"NotebookLM returned no source id for '{document.title}'.",
                hint="Check the notebook in NotebookLM, then run the sync again.",
            )
        return str(source_id)


def _source_record(source: Any) -> dict[str, Any]:
    """A library source as the plain record the rest of this tool reads."""
    kind = getattr(source, "kind", None)
    status = getattr(source, "status", None)
    return {
        "id": str(getattr(source, "id", "") or ""),
        "title": str(getattr(source, "title", "") or ""),
        "url": str(getattr(source, "url", "") or ""),
        "type": str(getattr(kind, "value", kind) or "").lower(),
        "status": str(getattr(status, "name", status) or "").lower(),
    }
