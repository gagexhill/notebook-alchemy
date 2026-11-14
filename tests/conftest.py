"""Shared fixtures. Every test runs in a throwaway working directory and home.

Real processes and real sockets are blocked, so a test cannot reach the developer's vault,
transcript cache, NotebookLM session, notebooks, or the network.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import platform
import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

# Rich reads these when the tool's consoles are made, at import: with one set, captured output
# is treated as a terminal and a few tests see colour codes inside the text they check.
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("TTY_COMPATIBLE", None)

import youtube_transcript_api  # noqa: F401

# The tool imports these on first use. Load them now, before the fixtures below replace
# subprocess.Popen: yt-dlp subclasses Popen while it imports.
import yt_dlp  # noqa: F401

# The Claude SDK reads platform details for its request headers; on Windows Python starts `ver` the
# first time. Read them now too, before the fixtures below block real processes.
platform.uname()
platform.system()
platform.release()
platform.machine()

_ENV_TO_CLEAR = (
    "ANTHROPIC_API_KEY",
    # The SDK also reads these; a developer's shell must not point a test at a real account.
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ALCHEMY_ENV",
    "ALCHEMY_LOG_LEVEL",
    "ALCHEMY_VAULT_PATH",
    "ALCHEMY_CAPTION_LANGUAGES",
    "ALCHEMY_SESSION_FILE",
    "ALCHEMY_CACHE_DIR",
    "ALCHEMY_MODEL",
    "ALCHEMY_MAX_CONCURRENCY",
    "ALCHEMY_NOTEBOOKLM_PROFILE",
    "ALCHEMY_COOKIES_BROWSER",
    "NOTEBOOKLM_AUTH_JSON",
    "NO_COLOR",
    # A proxy on this computer would carry a stray request out; none is used in tests.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NOTEBOOKLM_HOME",
    "NOTEBOOKLM_REFRESH_CMD",
)


from notebook_alchemy import client as _client_module  # noqa: E402

_REAL_DEFAULT_API = _client_module.default_api


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in _ENV_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)

    def _no_process(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"test tried to start a real process: {args[:1]}")

    real_connect = socket.socket.connect
    real_socketpair = socket.socketpair
    pairing = threading.local()

    def _socketpair(*args: object, **kwargs: object):
        # An event loop wakes itself through a pair of sockets on this computer, which on Windows
        # is made by connecting one to the other. That is not the network.
        pairing.active = True
        try:
            return real_socketpair(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            pairing.active = False

    def _no_network(sock: socket.socket, address: object, *args: object) -> None:
        if getattr(pairing, "active", False):
            return real_connect(sock, address, *args)  # type: ignore[arg-type]
        raise AssertionError("test tried to open a real network connection")

    monkeypatch.setattr(socket, "socketpair", _socketpair)

    monkeypatch.setattr(subprocess, "run", _no_process)
    # The background sign-in refresh talks to the system scheduler; tests get a fake Task Scheduler.
    scheduler = FakeScheduler()
    monkeypatch.setattr("notebook_alchemy.keepalive.PLATFORM", "win32")
    monkeypatch.setattr("notebook_alchemy.keepalive._default_runner", lambda: scheduler)
    monkeypatch.setattr(subprocess, "Popen", _no_process)

    def _no_notebooklm(profile: str | None, path: str | None) -> None:
        raise AssertionError("test tried to open a real NotebookLM session")

    monkeypatch.setattr("notebook_alchemy.client.default_api", _no_notebooklm)
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    # Real runs space caption requests seconds apart; tests do not wait.
    monkeypatch.setattr("notebook_alchemy.config.DEFAULT_CAPTION_SPACING", (0.0, 0.0))
    # The plan limit is a live NotebookLM read; it is unknown unless a test injects one.
    monkeypatch.setattr(
        "notebook_alchemy.client.NotebookLMClient._read_plan_limits_live", lambda self: None, raising=True
    )
    # .env loading writes to os.environ directly; put the environment back so nothing leaks onward.
    snapshot = dict(os.environ)
    yield SimpleNamespace(home=home, work=work, scheduler=scheduler)
    from notebook_alchemy import client

    client.close_all()  # each session has a background thread; none outlives its test
    os.environ.clear()
    os.environ.update(snapshot)


def real_default_api():
    """The tool's real opener for notebooklm-py, which every test otherwise has blocked."""
    return _REAL_DEFAULT_API


class FakeScheduler:
    """Stand-in for `schtasks`, `systemctl --user` and `crontab`: remembers what was registered."""

    def __init__(self, *, fail: str = "") -> None:
        self.tasks: dict[str, str] = {}  # schtasks name -> command
        self.crontab = ""
        self.units: set[str] = set()  # systemd timers enabled, launchd labels loaded
        self.calls: list[list[str]] = []
        self.fail = fail  # a command word ("/Create", "enable", ...) that exits 1

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        done = subprocess.CompletedProcess(cmd, 0, "", "")
        if self.fail and self.fail in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", f"{self.fail} refused")
        if cmd[0] == "schtasks":
            name = cmd[cmd.index("/TN") + 1]
            if "/Create" in cmd:  # the task definition, read now: the file is removed after the call
                self.tasks[name] = Path(cmd[cmd.index("/XML") + 1]).read_text(encoding="utf-16")
            elif "/Delete" in cmd:
                self.tasks.pop(name, None)
            elif name not in self.tasks:
                return subprocess.CompletedProcess(
                    cmd, 1, "", "ERROR: The system cannot find the file specified."
                )
        elif cmd[:1] == ["systemctl"] and len(cmd) > 3 and cmd[2] in ("enable", "disable", "is-enabled"):
            unit = cmd[-1]
            if cmd[2] == "enable":
                self.units.add(unit)
            elif cmd[2] == "disable":
                self.units.discard(unit)
            elif unit not in self.units:
                return subprocess.CompletedProcess(cmd, 1, "disabled", "")
        elif cmd[:1] == ["launchctl"]:
            if cmd[1] == "bootstrap":
                self.units.add(Path(cmd[3]).stem)
            elif cmd[1] == "bootout":
                self.units.discard(cmd[2].rsplit("/", 1)[-1])
            elif cmd[1] == "print" and cmd[2].rsplit("/", 1)[-1] not in self.units:
                return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
        elif cmd[:2] == ["crontab", "-l"]:
            if not self.crontab:
                return subprocess.CompletedProcess(cmd, 1, "", "no crontab for user")
            return subprocess.CompletedProcess(cmd, 0, self.crontab, "")
        elif cmd[:2] == ["crontab", "-"]:
            self.crontab = str(kwargs.get("input") or "")
        return done


class FakeProcess:
    """Scripted stand-in for `subprocess.run` calls to the notebooklm CLI."""

    def __init__(self, *rules: tuple[str, int, str, str]) -> None:
        # each rule: (substring that must appear in the joined command, returncode, stdout, stderr)
        self.rules = list(rules)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for needle, code, out, err in self.rules:
            if needle in joined:
                return subprocess.CompletedProcess(cmd, code, out, err)
        return subprocess.CompletedProcess(cmd, 1, "", f"no scripted response for: {joined}")


class FakeNotebookLM:
    """In-memory stand-in for notebooklm-py's Python API: notebooks and their sources keep state.

    Pass it as `api=` to `NotebookLMClient`, or set it as `notebook_alchemy.client.default_api`.
    `opened` records the (profile, session file) each session was opened with. `calls` records
    every request as words: `["list"]`, `["create", title]`, `["source", verb, notebook id, ...]`.

    `reset_titles` copies what the real service did: after processing, a source's title becomes its
    file name. `fail` maps a verb (`add`, `wait`, `rename`, `delete`, `add-async`, `fulltext`) to a
    substring of the request's words; a matching request raises the library's error, or its
    sign-in error when the verb is in `signed_out`.
    """

    def __init__(
        self,
        *,
        reset_titles: bool = False,
        fail: dict[str, str] | None = None,
        signed_out: tuple[str, ...] = (),
        delay: dict[str, float] | None = None,
    ) -> None:
        self.notebooks: list[dict[str, str]] = []
        self.sources: dict[str, list[dict[str, str]]] = {}
        self.reset_titles = reset_titles
        self.fail = fail or {}
        self.signed_out = signed_out
        self.calls: list[list[str]] = []
        self.opened: list[tuple[str | None, str | None]] = []
        self.uploaded: dict[str, str] = {}  # title -> the text of the file that was sent
        self.threads: set[str] = set()  # the threads requests ran on
        self.delay = delay or {}  # verb -> seconds a request takes, so requests can overlap
        self.before: Callable[[list[str]], None] | None = None  # runs first; may raise
        self.active = 0
        self.peak = 0  # the most requests under way at once
        self._n = 0
        self._lock = threading.Lock()  # the sync uploads from several threads
        self.api = SimpleNamespace(
            notebooks=_FakeNotebooks(self), sources=_FakeSources(self), settings=_FakeSettings()
        )

    def __call__(self, profile: str | None = None, path: str | None = None) -> _FakeContext:
        self.opened.append((profile, path))
        return _FakeContext(self.api)

    def seed(self, notebook_title: str, source_titles: list[str]) -> str:
        nb_id = self._new_id("nb")
        self.notebooks.append({"id": nb_id, "title": notebook_title})
        self.sources[nb_id] = [
            {"id": self._new_id("old"), "title": s, "status": "ready"} for s in source_titles
        ]
        return nb_id

    def notebook_id(self, notebook_title: str) -> str:
        return next(n["id"] for n in self.notebooks if n["title"] == notebook_title)

    def titles(self, notebook_title: str) -> list[str]:
        return sorted(s["title"] for s in self.sources[self.notebook_id(notebook_title)])

    def _new_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    def request(self, *words: str) -> None:
        """Record one request; raise when the test asked this one to fail."""
        import notebooklm.exceptions as lib

        self.calls.append(list(words))
        self.threads.add(threading.current_thread().name)
        if self.before is not None:
            self.before(list(words))
        verb = words[1] if words[0] == "source" else words[0]
        needle = self.fail.get(verb)
        if needle and needle in " ".join(words):
            if verb in self.signed_out:
                raise lib.AuthError("Authentication expired or invalid.")
            raise lib.NotebookLMError(f"{verb} failed on purpose")

    async def pause(self, verb: str) -> None:
        """Take as long as the test asked this verb to take, counting the requests under way."""
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay.get(verb, 0))
        finally:
            self.active -= 1

    @staticmethod
    def sign_in_ended() -> Exception:
        import notebooklm.exceptions as lib

        return lib.AuthError("Authentication expired or invalid.")


def _as_source(record: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        id=record["id"],
        title=record.get("title"),
        url=record.get("url"),
        kind=SimpleNamespace(value=record.get("type", "markdown")),
        status=SimpleNamespace(name=record.get("status", "ready").upper()),
    )


class _FakeContext:
    def __init__(self, api: SimpleNamespace) -> None:
        self.api = api

    async def __aenter__(self) -> SimpleNamespace:
        return self.api

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSettings:
    async def get_account_limits(self) -> SimpleNamespace:
        return SimpleNamespace(source_limit=None, notebook_limit=None, tier=None)


class _FakeNotebooks:
    def __init__(self, fake: FakeNotebookLM) -> None:
        self.fake = fake

    async def list(self) -> list[SimpleNamespace]:
        await self.fake.pause("list")
        with self.fake._lock:
            self.fake.request("list")
            return [SimpleNamespace(id=n["id"], title=n["title"]) for n in self.fake.notebooks]

    async def create(self, title: str) -> SimpleNamespace:
        await self.fake.pause("create")
        with self.fake._lock:
            self.fake.request("create", title)
            nb_id = self.fake._new_id("nb")
            self.fake.notebooks.append({"id": nb_id, "title": title})
            self.fake.sources[nb_id] = []
            return SimpleNamespace(id=nb_id, title=title)


class _FakeSources:
    def __init__(self, fake: FakeNotebookLM) -> None:
        self.fake = fake

    async def list(self, notebook_id: str) -> list[SimpleNamespace]:
        await self.fake.pause("list")
        with self.fake._lock:
            self.fake.request("source", "list", notebook_id)
            return [_as_source(s) for s in self.fake.sources[notebook_id]]

    async def add_file(
        self, notebook_id: str, file_path: object, mime_type: str | None = None, *, title: str | None = None
    ) -> SimpleNamespace:
        await self.fake.pause("add")
        path = pathlib.Path(str(file_path))
        assert path.is_file() and mime_type == "text/markdown"  # content travels as a file
        with self.fake._lock:
            self.fake.request("source", "add", notebook_id, path.name, title or "")
            self.fake.uploaded[title or path.name] = path.read_text(encoding="utf-8")
            source_id = self.fake._new_id("src")
            stored = path.name if self.fake.reset_titles else (title or path.name)
            self.fake.sources[notebook_id].append({"id": source_id, "title": stored, "status": "ready"})
            return SimpleNamespace(id=source_id, title=title)

    async def add_urls_async(self, notebook_id: str, urls: list[str]) -> list[SimpleNamespace]:
        await self.fake.pause("add-async")
        with self.fake._lock:
            self.fake.request("source", "add-async", notebook_id, *urls)
            added = []
            for url in urls:
                vid = url.rsplit("v=", 1)[-1]
                record = {
                    "id": self.fake._new_id("yt"),
                    "title": f"Video {vid}",
                    "url": url,
                    "status": "ready",
                    "type": "youtube",
                }
                self.fake.sources[notebook_id].append(record)
                added.append(_as_source(record))
            return added

    async def get_fulltext(self, notebook_id: str, source_id: str) -> SimpleNamespace:
        await self.fake.pause("fulltext")
        with self.fake._lock:
            self.fake.request("source", "fulltext", notebook_id, source_id)
            src = next(s for s in self.fake.sources[notebook_id] if s["id"] == source_id)
            url = src.get("url") or src.get("title", "")
            return SimpleNamespace(source_id=source_id, content=f"words spoken in {url}")

    async def wait_until_ready(self, notebook_id: str, source_id: str, timeout: float = 120.0) -> None:
        await self.fake.pause("wait")
        with self.fake._lock:
            self.fake.request("source", "wait", notebook_id, source_id)

    async def rename(self, notebook_id: str, source_id: str, new_title: str) -> None:
        await self.fake.pause("rename")
        with self.fake._lock:
            self.fake.request("source", "rename", notebook_id, source_id, new_title)
            next(s for s in self.fake.sources[notebook_id] if s["id"] == source_id)["title"] = new_title

    async def delete(self, notebook_id: str, source_id: str) -> None:
        await self.fake.pause("delete")
        with self.fake._lock:
            self.fake.request("source", "delete", notebook_id, source_id)
            bucket = self.fake.sources[notebook_id]
            self.fake.sources[notebook_id] = [s for s in bucket if s["id"] != source_id]
