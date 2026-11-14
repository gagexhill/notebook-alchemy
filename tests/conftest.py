"""Shared fixtures. Every test runs in a throwaway working directory and home.

Real processes and real sockets are blocked, so a test cannot reach the developer's vault,
transcript cache, NotebookLM session, notebooks, or the network.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import youtube_transcript_api  # noqa: F401

# The tool imports these on first use. Load them now, before the fixtures below replace
# subprocess.Popen: yt-dlp subclasses Popen while it imports.
import yt_dlp  # noqa: F401

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
)


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

    def _no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("test tried to open a real network connection")

    monkeypatch.setattr(subprocess, "run", _no_process)
    # The background sign-in refresh talks to the system scheduler; tests get a fake Task Scheduler.
    scheduler = FakeScheduler()
    monkeypatch.setattr("notebook_alchemy.keepalive.PLATFORM", "win32")
    monkeypatch.setattr("notebook_alchemy.keepalive._default_runner", lambda: scheduler)
    monkeypatch.setattr(subprocess, "Popen", _no_process)
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
    os.environ.clear()
    os.environ.update(snapshot)


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
    """In-memory stand-in for the `notebooklm` CLI: notebooks and their sources keep state.

    `reset_titles` copies what the real service did: after processing, a source's title becomes its
    file name. `fail` maps a command word (`add`, `wait`, `rename`, `delete`) to a substring of the
    command line; a matching call exits non-zero.
    """

    def __init__(self, *, reset_titles: bool = False, fail: dict[str, str] | None = None) -> None:
        self.notebooks: list[dict[str, str]] = []
        self.sources: dict[str, list[dict[str, str]]] = {}
        self.reset_titles = reset_titles
        self.fail = fail or {}
        self.calls: list[list[str]] = []
        self._n = 0
        self._lock = threading.Lock()  # the sync uploads from several threads

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

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        with self._lock:
            return self._handle(cmd)

    def _handle(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        args = [a for a in cmd[1:] if a != "--json"]
        if "--storage" in args:
            i = args.index("--storage")
            del args[i : i + 2]
        del args[:2]  # --profile NAME
        joined = " ".join(cmd)
        verb = args[1] if args[0] == "source" else args[0]
        needle = self.fail.get(verb)
        if needle and needle in joined:
            return subprocess.CompletedProcess(cmd, 2, "", f"{verb} failed on purpose")

        def option(name: str) -> str:
            return args[args.index(name) + 1]

        def done(payload: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

        if args[0] == "list":
            return done({"notebooks": self.notebooks, "count": len(self.notebooks)})
        if args[0] == "create":
            nb_id = self._new_id("nb")
            self.notebooks.append({"id": nb_id, "title": args[1]})
            self.sources[nb_id] = []
            return done({"id": nb_id})
        nb_id = option("-n")
        bucket = self.sources[nb_id]
        if verb == "list":
            return done(list(bucket))
        if verb == "add":
            path = args[2]
            source_id = self._new_id("src")
            stored = pathlib.Path(path).name if self.reset_titles else option("--title")
            bucket.append({"id": source_id, "title": stored, "status": "ready"})
            return done({"source": {"id": source_id, "title": option("--title")}})
        if verb == "add-async":
            for url in [a for a in args[2:] if a.startswith("http")]:
                vid = url.rsplit("v=", 1)[-1]
                bucket.append(
                    {
                        "id": self._new_id("yt"),
                        "title": f"Video {vid}",
                        "url": url,
                        "status": "ready",
                        "type": "youtube",
                    }
                )
            return done({"queued": True})
        if verb == "fulltext":
            src = next(s for s in bucket if s["id"] == args[2])
            url = src.get("url") or src.get("title", "")
            return done({"source_id": src["id"], "content": f"words spoken in {url}"})
        if verb == "wait":
            return subprocess.CompletedProcess(cmd, 0, "ready", "")
        if verb == "rename":
            next(s for s in bucket if s["id"] == args[2])["title"] = args[3]
            return done({})
        if verb == "delete":
            self.sources[nb_id] = [s for s in bucket if s["id"] != args[2]]
            return done({})
        return subprocess.CompletedProcess(cmd, 1, "", f"unscripted: {joined}")
