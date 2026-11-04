"""Shared fixtures. Every test runs in a throwaway working directory and home.

Real processes and real sockets are blocked, so a test cannot reach the developer's vault,
transcript cache, NotebookLM session, notebooks, or the network.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_ENV_TO_CLEAR = (
    "GEMINI_API_KEY",
    "ALCHEMY_ENV",
    "ALCHEMY_LOG_LEVEL",
    "ALCHEMY_VAULT_PATH",
    "ALCHEMY_STATE_DIR",
    "ALCHEMY_SESSION_FILE",
    "ALCHEMY_CACHE_DIR",
    "ALCHEMY_GEMINI_MODEL",
    "ALCHEMY_MAX_CONCURRENCY",
    "NOTEBOOKLM_AUTH_JSON",
    "NO_COLOR",
)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
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
    monkeypatch.setattr(subprocess, "Popen", _no_process)
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    return SimpleNamespace(home=home, work=work)


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
