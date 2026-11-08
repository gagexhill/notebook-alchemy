"""Keeping the NotebookLM sign-in alive: the scheduled refresh on each system, and the refresh itself."""

from __future__ import annotations

import datetime as dt
import json
import plistlib
import subprocess
import tempfile
from pathlib import Path

import pytest

from notebook_alchemy import keepalive, ui
from notebook_alchemy.errors import AlchemyError, ConfigError
from notebook_alchemy.keepalive import INTERVAL_MINUTES, KeepAlive, read_status, refresh, task_name

from .conftest import FakeScheduler


def test_windows_registers_reads_back_and_removes_a_task(tmp_path: Path) -> None:
    scheduler = FakeScheduler()
    keeper = KeepAlive("personal", tmp_path / "home & co", runner=scheduler, platform="win32")
    assert not keeper.state().on
    assert keeper.enable() == "Task Scheduler"
    definition = scheduler.tasks[task_name("personal")]
    assert f"<Interval>PT{INTERVAL_MINUTES}M</Interval>" in definition
    # A laptop on battery still runs it, and a missed run (asleep) happens on waking.
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in definition
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in definition
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in definition
    assert "-m notebook_alchemy.keepalive --profile personal --home" in definition
    assert "home &amp; co" in definition  # escaped for XML
    assert not list(Path(tempfile.gettempdir()).glob("notebook-alchemy-task-*.xml"))  # not left behind
    assert keeper.state().on and keeper.state().where == "Task Scheduler"
    assert keeper.disable() and not scheduler.tasks and not keeper.disable()


def test_a_task_that_cannot_be_read_back_is_an_error(tmp_path: Path) -> None:
    scheduler = FakeScheduler(fail="/Query")
    with pytest.raises(AlchemyError, match="could not be read back"):
        KeepAlive("default", tmp_path, runner=scheduler, platform="win32").enable()


def test_a_refused_registration_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(AlchemyError, match="register the Task Scheduler task: /Create refused"):
        KeepAlive("default", tmp_path, runner=FakeScheduler(fail="/Create"), platform="win32").enable()


def test_a_profile_name_that_is_not_safe_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        KeepAlive('x" & calc', tmp_path, runner=FakeScheduler(), platform="win32")


def test_macos_writes_and_removes_a_launchd_agent(tmp_path: Path) -> None:
    scheduler = FakeScheduler()
    keeper = KeepAlive("default", tmp_path / "home", runner=scheduler, platform="darwin", user_home=tmp_path)
    assert keeper.enable() == "launchd"
    plist = tmp_path / "Library" / "LaunchAgents" / "com.notebook-alchemy.keepalive.default.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["StartInterval"] == INTERVAL_MINUTES * 60 and data["ProgramArguments"][1:3] == [
        "-m",
        "notebook_alchemy.keepalive",
    ]
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in scheduler.calls)
    assert keeper.disable() and not plist.exists()


def test_linux_uses_a_systemd_user_timer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keepalive.shutil, "which", lambda name: f"/usr/bin/{name}")
    scheduler = FakeScheduler()
    keeper = KeepAlive("default", tmp_path / "home", runner=scheduler, platform="linux", user_home=tmp_path)
    assert keeper.enable() == "systemd"
    units = tmp_path / ".config" / "systemd" / "user"
    timer = (units / "notebook-alchemy-keepalive-default.timer").read_text(encoding="utf-8")
    assert f"OnUnitActiveSec={INTERVAL_MINUTES}min" in timer
    assert "notebook_alchemy.keepalive" in (units / "notebook-alchemy-keepalive-default.service").read_text()
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "notebook-alchemy-keepalive-default.timer",
    ] in scheduler.calls
    assert keeper.disable() and not any(units.iterdir())


def test_linux_without_systemd_uses_cron_and_keeps_other_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(keepalive.shutil, "which", lambda name: None)
    scheduler = FakeScheduler()
    scheduler.crontab = "0 3 * * * backup.sh\n"
    keeper = KeepAlive("default", tmp_path / "home", runner=scheduler, platform="linux")
    assert keeper.enable() == "cron"
    lines = scheduler.crontab.splitlines()
    assert lines[0] == "0 3 * * * backup.sh" and lines[1].startswith(f"*/{INTERVAL_MINUTES} * * * * ")
    keeper.enable()  # again: replaced, not added twice
    assert len(scheduler.crontab.splitlines()) == 2
    assert keeper.disable() and scheduler.crontab.splitlines() == ["0 3 * * * backup.sh"]


# --- the refresh itself -----------------------------------------------------------------------------


def test_a_refresh_records_whether_it_worked(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def ok(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert refresh("personal", tmp_path, runner=ok)
    assert seen[0] == [
        "notebooklm",
        "--profile",
        "personal",
        "auth",
        "refresh",
        "--quiet",
        "--allow-headless",
    ]
    assert read_status(tmp_path)["personal"]["ok"] is True

    def expired(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "Authentication expired.\n Run notebooklm login")

    assert not refresh("personal", tmp_path, runner=expired)
    last = read_status(tmp_path)["personal"]
    assert last["ok"] is False and last["detail"] == "Authentication expired. Run notebooklm login"


def test_a_refresh_without_notebooklm_installed_is_recorded_as_failed(tmp_path: Path) -> None:
    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    assert not refresh("default", tmp_path, runner=missing)
    assert "not found" in read_status(tmp_path)["default"]["detail"]


def test_status_shows_when_the_last_refresh_ran(tmp_path: Path) -> None:
    from notebook_alchemy.cli import _keepalive_line

    scheduler = FakeScheduler()
    keeper = KeepAlive("default", tmp_path, runner=scheduler, platform="win32")
    keeper.enable()
    assert _keepalive_line(keeper.state()) == (
        True,
        "The sign-in is refreshed in the background (no refresh has run yet).",
    )
    at = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=7)).isoformat(timespec="seconds")
    (tmp_path / "keepalive.json").write_text(
        json.dumps({"default": {"at": at, "ok": False, "detail": "expired"}}), encoding="utf-8"
    )
    ok, text = _keepalive_line(keeper.state())
    assert not ok and "7 minutes ago" in text and "expired" in text


def test_ago_reads_like_speech() -> None:
    now = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC)
    assert ui.ago("2026-11-08T11:59:30+00:00", now) == "just now"
    assert ui.ago("2026-11-08T11:00:00+00:00", now) == "1 hour ago"
    assert ui.ago("2026-11-06T12:00:00+00:00", now) == "2 days ago"
    assert ui.ago("not a time", now) == "not a time"
