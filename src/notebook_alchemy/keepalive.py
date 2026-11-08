"""Keeping the NotebookLM sign-in alive between runs.

Google's session cookie stays valid only for minutes to hours unless it is rotated, so a sign-in
left idle overnight is dead by morning. notebooklm-py's guidance is to run `notebooklm auth refresh`
every 15 to 20 minutes from the operating system's scheduler. This module registers that task, per
NotebookLM profile, with the scheduler each system already has:

- Windows: a Task Scheduler task for the current user, run by `pythonw` so no window opens, and
  allowed to run on battery.
- macOS: a launchd agent in `~/Library/LaunchAgents`.
- Linux: a systemd user timer, or a crontab line when systemd is not available.

The task runs `python -m notebook_alchemy.keepalive`, which runs `python -m notebooklm auth refresh` (by
the same Python, so the scheduler's short PATH does not matter) and writes
the outcome to `keepalive.json` in the working folder, so `status` shows whether the refresh is
actually succeeding rather than only whether the task exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .config import validate_profile_name
from .errors import AlchemyError

INTERVAL_MINUTES = 20
STATUS_FILE = "keepalive.json"
REFRESH_TIMEOUT = 180
# Longest a single refresh may run before Task Scheduler stops it.
TASK_TIME_LIMIT = "PT10M"
# `status` calls the refresh stale when this many runs in a row were missed.
STALE_AFTER_MISSED = 3
# Characters a scheduler would expand or split in a path: Task Scheduler %VAR%, systemd % and $,
# cron % (a newline) and ` in a shell line.
UNSAFE_PATH_CHARACTERS = frozenset("%$`")

Runner = Callable[..., subprocess.CompletedProcess]
PLATFORM = sys.platform  # read through this name so tests can pick a system


def _default_runner() -> Runner:
    return subprocess.run


@dataclass
class KeepAliveState:
    on: bool
    where: str  # "Task Scheduler", "launchd", "systemd", "cron", or "" when off
    last_at: str = ""  # ISO time of the last refresh the task ran
    last_ok: bool | None = None
    last_detail: str = ""
    stale: bool = False  # on, but no refresh recorded for STALE_AFTER_MISSED intervals
    last_worked_at: str = ""  # the last refresh that worked, so a failure shows how long it has lasted


def task_name(profile: str) -> str:
    return f"Notebook Alchemy keep signed in ({profile})"


def _label(profile: str) -> str:
    return f"com.notebook-alchemy.keepalive.{profile}"


def _unit(profile: str) -> str:
    return f"notebook-alchemy-keepalive-{profile}"


def _python(windowless: bool) -> str:
    exe = Path(sys.executable)
    if windowless:
        quiet = exe.with_name("pythonw.exe")
        if quiet.is_file():
            return str(quiet)
    return str(exe)


def task_command(profile: str, home: Path, *, windowless: bool = False) -> list[str]:
    """What the scheduler runs every INTERVAL_MINUTES."""
    return [
        _python(windowless),
        "-m",
        "notebook_alchemy.keepalive",
        "--profile",
        profile,
        "--home",
        str(home),
    ]


def task_xml(command: list[str], *, start: dt.datetime | None = None) -> str:
    """The Task Scheduler definition: every INTERVAL_MINUTES for the signed-in user, on battery too,
    caught up after sleep, one run at a time, stopped after TASK_TIME_LIMIT."""
    begin = (start or dt.datetime.now()).replace(microsecond=0).isoformat()
    arguments = subprocess.list2cmdline(command[1:])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Keeps the NotebookLM sign-in from expiring between Notebook Alchemy runs.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{begin}</StartBoundary>
      <Repetition>
        <Interval>PT{INTERVAL_MINUTES}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>{TASK_TIME_LIMIT}</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command[0])}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


class KeepAlive:
    """Registers, removes and reports the refresh task for one NotebookLM profile."""

    def __init__(
        self,
        profile: str,
        home: Path,
        *,
        runner: Runner | None = None,
        platform: str | None = None,
        user_home: Path | None = None,
    ) -> None:
        self.profile = validate_profile_name(profile, source="--profile")
        self.home = home
        self.run = runner or _default_runner()
        self.platform = platform or PLATFORM
        self.user_home = user_home or Path.home()

    # -- the scheduler for this system --

    @property
    def kind(self) -> str:
        if self.platform.startswith("win"):
            return "Task Scheduler"
        if self.platform == "darwin":
            return "launchd"
        # On Linux the choice depends on the session (a desktop has systemd --user, an SSH or
        # container shell may not), so the scheduler an entry was made with is recorded and reused.
        recorded = read_status(self.home).get(self.profile, {}).get("where")
        if recorded in ("systemd", "cron"):
            return str(recorded)
        return "systemd" if shutil.which("systemctl") and self._systemd_user_ok() else "cron"

    def _systemd_user_ok(self) -> bool:
        try:
            res = self.run(
                ["systemctl", "--user", "show-environment"], capture_output=True, text=True, timeout=20
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return res.returncode == 0

    def _call(self, cmd: list[str], what: str, *, stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            res = self.run(cmd, capture_output=True, text=True, timeout=60, input=stdin)
        except FileNotFoundError as exc:
            raise AlchemyError(f"Could not {what}: `{cmd[0]}` was not found.") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AlchemyError(f"Could not {what}: {exc}") from exc
        return res

    def _check(self, res: subprocess.CompletedProcess, what: str) -> None:
        if res.returncode != 0:
            detail = " ".join(((res.stderr or "") + " " + (res.stdout or "")).split())[:300]
            raise AlchemyError(
                f"Could not {what}: {detail or f'exit code {res.returncode}'}",
                hint="Run the same command again; if it keeps failing, sign in again before each run.",
            )

    # -- on / off / state --

    def enable(self) -> str:
        """Register the task (replacing an earlier one) and return the scheduler's name."""
        kind = self.kind
        command = task_command(self.profile, self.home, windowless=kind == "Task Scheduler")
        unsafe = sorted({c for arg in command for c in arg if c in UNSAFE_PATH_CHARACTERS})
        if unsafe:
            # Task Scheduler expands %VAR%, systemd expands % and $, and cron turns % into a newline.
            raise AlchemyError(
                f"The path to Python or to the working folder contains {' '.join(unsafe)}, which "
                f"{kind} would change, so the refresh would not run.",
                hint="Install Notebook Alchemy (or set ALCHEMY_CACHE_DIR) in a folder without those "
                "characters, or sign in again before each run.",
            )
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            if kind == "Task Scheduler":
                self._enable_windows(command)
            elif kind == "launchd":
                self._enable_launchd(command)
            elif kind == "systemd":
                self._enable_systemd(command)
            else:
                self._enable_cron(command)
            if not self.state().on:
                raise AlchemyError(
                    f"The {kind} entry was written but is not active.",
                    hint="Run `notebook-alchemy keep-signed-in` again.",
                )
        except AlchemyError:
            self._remove_files()  # nothing half-made is left to look like "on"
            raise
        _record(self.home, self.profile, where=kind)
        return kind

    def _enable_windows(self, command: list[str]) -> None:
        # Registered from a task definition, not `schtasks /SC MINUTE`: only the definition can let
        # the task run on battery (the default refuses, and a laptop's task then waits, "Queued",
        # until it is plugged in) and catch up after sleep.
        fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix="notebook-alchemy-task-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(task_xml(command).encode("utf-16"))
            res = self._call(
                ["schtasks", "/Create", "/F", "/TN", task_name(self.profile), "/XML", xml_path],
                "register the Task Scheduler task",
            )
        finally:
            Path(xml_path).unlink(missing_ok=True)
        self._check(res, "register the Task Scheduler task")

    def _enable_launchd(self, command: list[str]) -> None:
        plist = self._plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        self._launchctl_bootout()
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": _label(self.profile),
                    "ProgramArguments": command,
                    "StartInterval": INTERVAL_MINUTES * 60,
                    "RunAtLoad": True,
                }
            )
        )
        res = self._call(["launchctl", "bootstrap", f"gui/{_uid()}", str(plist)], "start the launchd agent")
        self._check(res, "start the launchd agent")

    def _enable_systemd(self, command: list[str]) -> None:
        folder = self._systemd_dir()
        folder.mkdir(parents=True, exist_ok=True)
        line = " ".join(_systemd_quote(a) for a in command)
        (folder / f"{_unit(self.profile)}.service").write_text(
            f"[Unit]\nDescription={task_name(self.profile)}\n\n[Service]\nType=oneshot\nExecStart={line}\n",
            encoding="utf-8",
        )
        (folder / f"{_unit(self.profile)}.timer").write_text(
            f"[Unit]\nDescription={task_name(self.profile)}\n\n[Timer]\nOnBootSec=2min\n"
            f"OnUnitActiveSec={INTERVAL_MINUTES}min\n\n[Install]\nWantedBy=timers.target\n",
            encoding="utf-8",
        )
        self._check(self._call(["systemctl", "--user", "daemon-reload"], "reload systemd"), "reload systemd")
        res = self._call(
            ["systemctl", "--user", "enable", "--now", f"{_unit(self.profile)}.timer"],
            "start the systemd timer",
        )
        self._check(res, "start the systemd timer")

    def _enable_cron(self, command: list[str]) -> None:
        lines = [ln for ln in self._crontab() if not ln.endswith(self._cron_marker())]
        line = " ".join(_shell_quote(a) for a in command)
        lines.append(f"*/{INTERVAL_MINUTES} * * * * {line} >/dev/null 2>&1 {self._cron_marker()}")
        res = self._call(["crontab", "-"], "update the crontab", stdin="\n".join(lines) + "\n")
        self._check(res, "update the crontab")

    def disable(self) -> bool:
        """Remove the task; True when there was one (active, or its files left behind)."""
        kind = self.kind
        had_files = self._has_files()
        if not self.state().on and not had_files:
            return False
        if kind == "Task Scheduler":
            res = self._call(["schtasks", "/Delete", "/F", "/TN", task_name(self.profile)], "remove the task")
            self._check(res, "remove the Task Scheduler task")
        elif kind == "launchd":
            self._launchctl_bootout()
        elif kind == "systemd":
            res = self._call(
                ["systemctl", "--user", "disable", "--now", f"{_unit(self.profile)}.timer"],
                "stop the systemd timer",
            )
            if res.returncode != 0 and self._systemd_active():
                self._check(res, "stop the systemd timer")  # files removed by hand: nothing to stop
        else:
            lines = [ln for ln in self._crontab() if not ln.endswith(self._cron_marker())]
            res = self._call(
                ["crontab", "-"], "update the crontab", stdin="\n".join(lines) + "\n" if lines else ""
            )
            self._check(res, "update the crontab")
        self._remove_files()
        if self.state().on:
            raise AlchemyError(f"The {kind} entry is still there after removing it.")
        _record(self.home, self.profile, where=None)
        return True

    def state(self) -> KeepAliveState:
        """Whether the scheduler has the entry *active* (not only its files), and the last refresh."""
        kind = self.kind
        if kind == "Task Scheduler":
            res = self._call(
                ["schtasks", "/Query", "/TN", task_name(self.profile)], "read the Task Scheduler"
            )
            on = res.returncode == 0
        elif kind == "launchd":
            res = self._call(["launchctl", "print", f"gui/{_uid()}/{_label(self.profile)}"], "read launchd")
            on = res.returncode == 0
        elif kind == "systemd":
            on = self._systemd_active()
        else:
            on = any(ln.endswith(self._cron_marker()) for ln in self._crontab())
        last = read_status(self.home).get(self.profile, {})
        at = str(last.get("at", ""))
        return KeepAliveState(
            on,
            kind if on else "",
            at,
            last.get("ok") if isinstance(last.get("ok"), bool) else None,
            str(last.get("detail", "")),
            stale=on and _older_than(at, INTERVAL_MINUTES * STALE_AFTER_MISSED),
            last_worked_at=str(last.get("worked_at", "")),
        )

    # -- per-system details --

    def _systemd_active(self) -> bool:
        res = self._call(
            ["systemctl", "--user", "is-enabled", f"{_unit(self.profile)}.timer"], "read systemd"
        )
        return res.returncode == 0

    def _has_files(self) -> bool:
        if self.kind == "launchd":
            return self._plist_path().is_file()
        if self.kind == "systemd":
            return any(
                (self._systemd_dir() / f"{_unit(self.profile)}{s}").is_file() for s in (".service", ".timer")
            )
        return False

    def _remove_files(self) -> None:
        if self.kind == "launchd":
            self._plist_path().unlink(missing_ok=True)
        elif self.kind == "systemd":
            for suffix in (".service", ".timer"):
                (self._systemd_dir() / f"{_unit(self.profile)}{suffix}").unlink(missing_ok=True)
            # Best effort: systemd forgets removed units on its next reload in any case.
            self._call(["systemctl", "--user", "daemon-reload"], "reload systemd")

    def _plist_path(self) -> Path:
        return self.user_home / "Library" / "LaunchAgents" / f"{_label(self.profile)}.plist"

    def _launchctl_bootout(self) -> None:
        # Not loaded is the usual reason this fails; state() reads back what launchd really has.
        self._call(["launchctl", "bootout", f"gui/{_uid()}/{_label(self.profile)}"], "stop the launchd agent")

    def _systemd_dir(self) -> Path:
        return self.user_home / ".config" / "systemd" / "user"

    def _cron_marker(self) -> str:
        return f"# notebook-alchemy keepalive {self.profile}"

    def _crontab(self) -> list[str]:
        res = self._call(["crontab", "-l"], "read the crontab")
        if res.returncode != 0:
            if "no crontab" in (res.stderr or "").lower():
                return []
            self._check(res, "read the crontab")
        return [ln for ln in (res.stdout or "").splitlines() if ln.strip()]


def _uid() -> int:
    return getattr(os, "getuid", lambda: 0)()  # launchd is only used on macOS, where getuid exists


def _shell_quote(arg: str) -> str:
    return "'" + arg.replace("'", "'\\''") + "'"


def _systemd_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _older_than(iso: str, minutes: int) -> bool:
    try:
        then = dt.datetime.fromisoformat(iso)
    except ValueError:
        return False  # no refresh recorded yet is reported as such, not as stale
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.UTC)
    return dt.datetime.now(dt.UTC) - then > dt.timedelta(minutes=minutes)


# --- the task itself --------------------------------------------------------------------------------


def read_status(home: Path) -> dict[str, Any]:
    try:
        data = json.loads((home / STATUS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}  # an unreadable status file only means "no refresh recorded"; refresh rewrites it
    return data if isinstance(data, dict) else {}


def _record(home: Path, profile: str, **fields: Any) -> None:
    """Update this profile's entry in the status file; a field set to None is removed.

    The working folder is not created here: after `clean` removed it, the refresh keeps running
    (it is what keeps the sign-in alive) without bringing the folder back.
    """
    if not home.is_dir():
        return
    data = read_status(home)
    entry = data.get(profile) if isinstance(data.get(profile), dict) else {}
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    data[profile] = entry
    try:
        (home / STATUS_FILE).write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        print(f"Could not record the refresh: {exc}", file=sys.stderr)


def _module_python() -> str:
    """The Python that runs `notebooklm` as a module: no PATH needed (launchd, cron and systemd
    give a task a minimal PATH), and a console Python even when the task itself runs under pythonw."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console) if exe.name.lower() == "pythonw.exe" and console.is_file() else str(exe)


def refresh(profile: str, home: Path, *, runner: Runner | None = None) -> bool:
    """Run `notebooklm auth refresh` once and record the outcome; True when it worked."""
    profile = validate_profile_name(profile, source="--profile")
    cmd = [_module_python(), "-m", "notebooklm", "--profile", profile, "auth", "refresh", "--quiet"]
    cmd.append("--allow-headless")
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": REFRESH_TIMEOUT}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = (runner or subprocess.run)(cmd, **kwargs)
        ok = res.returncode == 0
        detail = "" if ok else " ".join(((res.stderr or "") + " " + (res.stdout or "")).split())
        # notebooklm's own advice names its command; `status` gives this tool's sign-in command instead.
        detail = re.sub(r"\s*Run 'notebooklm login'[^.]*\.?", "", detail).strip()[:300]
    except FileNotFoundError:
        ok, detail = False, "Python could not be started to run notebooklm"
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, detail = False, str(exc)[:300]
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    _record(home, profile, at=now, ok=ok, detail=detail, **({"worked_at": now} if ok else {}))
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m notebook_alchemy.keepalive")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--home", type=Path, default=Path.home() / ".notebook-alchemy")
    args = parser.parse_args(argv)
    return 0 if refresh(args.profile, args.home) else 1


if __name__ == "__main__":
    sys.exit(main())
