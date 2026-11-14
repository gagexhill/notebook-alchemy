"""Command line behaviour: output, exit codes, safety."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from rich.console import Console

from notebook_alchemy import __version__, ui
from notebook_alchemy import cli as cli_module
from notebook_alchemy.channel import (
    ChannelScanner,
    ChannelScanResult,
    ChannelVideo,
    TranscriptManager,
    TranscriptResult,
)
from notebook_alchemy.cli import cli
from notebook_alchemy.client import (
    AccountIdentity,
    NotebookLMClient,
    PlanLimits,
    SessionStatus,
    consent_file,
    record_upload_consent,
    upload_confirmed,
)
from notebook_alchemy.engine import AlchemyEngine, SyncResult
from notebook_alchemy.errors import AlchemyError, ChannelScanError, SyncError

from .conftest import FakeNotebookLM


@pytest.fixture(autouse=True)
def reset_ui() -> None:
    yield
    ui.console = Console(highlight=False)
    ui.err_console = Console(stderr=True, highlight=False)
    ui.UiState.quiet = ui.UiState.debug = False
    ui.UiState.verbose = 0


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


ACCOUNT = "owner@example.test"


@pytest.fixture(autouse=True)
def known_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every profile reads as signed in to ACCOUNT unless a test says otherwise."""
    monkeypatch.setattr(
        NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, ACCOUNT)
    )


def _scan_result(*titles: str) -> ChannelScanResult:
    videos = [
        ChannelVideo(f"vid{i:08d}", t, f"https://www.youtube.com/watch?v=vid{i:08d}", 120)
        for i, t in enumerate(titles)
    ]
    return ChannelScanResult("My Channel", videos)


@pytest.fixture
def fake_channel(monkeypatch: pytest.MonkeyPatch):
    def install(*titles: str) -> None:
        monkeypatch.setattr(ChannelScanner, "scan", lambda self, url, limit=None, **kw: _scan_result(*titles))

    return install


# --- help and usage -------------------------------------------------------------------------------


def test_help_lists_commands_in_first_run_order(runner: CliRunner) -> None:
    out = runner.invoke(cli, ["--help"]).output
    order = [
        out.index(f"\n  {name}") for name in ("run", "dry-run", "status", "login", "setup", "clean", "test")
    ]
    assert order == sorted(order)
    assert "First run:" in out and "\n  sync" not in out and "\n  append" not in out  # removed


@pytest.mark.parametrize("command", ["run", "dry-run", "login", "setup", "clean"])
def test_every_command_help_has_examples(runner: CliRunner, command: str) -> None:
    result = runner.invoke(cli, [command, "--help"])
    assert result.exit_code == 0 and "Example" in result.output


def test_version(runner: CliRunner) -> None:
    assert __version__ in runner.invoke(cli, ["--version"]).output


@pytest.mark.parametrize(
    "args",
    [
        ["dry-run"],
        ["run"],
        ["dry-run", "--mock", "--limit", "0"],
        ["run", "--mock", "--max-per-vol", "0"],
    ],
)
def test_missing_or_invalid_input_is_a_usage_error(runner: CliRunner, args: list[str]) -> None:
    result = runner.invoke(cli, args)
    assert result.exit_code == 2
    assert "Traceback" not in result.output and "Unexpected error" not in result.output


def test_no_source_hint_mentions_mock(runner: CliRunner) -> None:
    assert "--mock" in runner.invoke(cli, ["dry-run"]).output


# --- dry-run --------------------------------------------------------------------------------------


def test_dry_run_scan_failure_names_cause_and_next_step(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self, url, limit=None, **kw):
        raise ChannelScanError("YouTube could not load 'x': HTTP Error 404", hint="Check the URL.")

    monkeypatch.setattr(ChannelScanner, "scan", boom)
    result = runner.invoke(cli, ["dry-run", "@gone"])
    assert result.exit_code == 1
    assert "404" in result.output and "Next:" in result.output and "Check the URL" in result.output
    assert "Example Notebook" not in result.output and "Sample" not in result.output


# --- run ------------------------------------------------------------------------------------------


def test_unexpected_errors_are_contained(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self, *a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ChannelScanner, "scan", boom)
    plain = runner.invoke(cli, ["dry-run", "@x"])
    assert plain.exit_code == 70 and "RuntimeError: kaboom" in plain.output and "--debug" in plain.output
    assert "Traceback" not in plain.output
    debug = runner.invoke(cli, ["--debug", "dry-run", "@x"])
    assert "Traceback" in debug.output


# --- status / login -------------------------------------------------------------------------------


def test_status_is_offline_and_actionable(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NotebookLMClient, "cli_version", lambda self: None)
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "pip install notebooklm-py" in result.output and "notebook-alchemy login" in result.output
    assert "notebook-alchemy setup" in result.output and "AI sorting is off" in result.output


def test_login_check_states(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        NotebookLMClient, "check_session", lambda self: SessionStatus(False, "Session expired.")
    )
    bad = runner.invoke(cli, ["login", "--check"])
    assert bad.exit_code == 1 and "Session expired." in bad.output and "notebook-alchemy login" in bad.output
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 7))
    good = runner.invoke(cli, ["login", "--check"])
    assert good.exit_code == 0 and "7 notebook" in good.output


def test_login_without_the_notebooklm_command(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    def absent(*a, **k):
        raise FileNotFoundError("notebooklm")

    monkeypatch.setattr(subprocess, "run", absent)
    result = runner.invoke(cli, ["login"])
    assert result.exit_code == 1 and "pip install notebooklm-py" in result.output
    assert "Traceback" not in result.output


def test_login_cookie_failure_does_not_fall_through(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fail(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "cookies unreadable")

    monkeypatch.setattr(subprocess, "run", fail)
    result = runner.invoke(cli, ["login", "--browser-cookies", "edge"])
    assert result.exit_code == 1 and "cookies unreadable" in result.output
    assert len(calls) == 1  # did not open an interactive browser afterwards


def test_a_long_sign_in_failure_shows_its_cause(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    notes = "Note: sibling-product domains are not requested by default. " * 8
    out = notes + "Reading cookies from chrome... Could not read chrome cookies: browser database is locked."
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", out))
    result = runner.invoke(cli, ["login", "--browser-cookies", "chrome"], terminal_width=200)
    assert result.exit_code == 1 and "database is locked" in " ".join(result.output.split())


# --- clean ----------------------------------------------------------------------------------------


def _seed_cache_and_vault(env) -> tuple[Path, Path]:
    cache = env.home / ".notebook-alchemy" / "cache"
    cache.mkdir(parents=True)
    record = {"video_id": "abc", "full_text": "x" * 2048}
    (cache / "abc.json").write_text(json.dumps(record), encoding="utf-8")
    vault = env.work / "vault" / "chan" / "compendiums"
    vault.mkdir(parents=True)
    (vault / "A.md").write_text("built", encoding="utf-8")
    (vault / "_INDEX.md").write_text("index", encoding="utf-8")
    return cache, vault


def test_clean_defaults_to_the_cache_and_keeps_vaults(runner: CliRunner, isolated_environment) -> None:
    cache, vault = _seed_cache_and_vault(isolated_environment)
    result = runner.invoke(cli, ["clean"])
    assert result.exit_code == 0 and "freed" in result.output
    assert not list(cache.iterdir()) and (vault / "A.md").exists()


def test_clean_vaults_asks_first(runner: CliRunner, isolated_environment) -> None:
    _cache, vault = _seed_cache_and_vault(isolated_environment)
    declined = runner.invoke(cli, ["clean", "--vaults"], input="n\n")
    assert declined.exit_code == 0 and "Nothing was deleted" in declined.output and (vault / "A.md").exists()
    accepted = runner.invoke(cli, ["clean", "--vaults"], input="y\n")
    assert accepted.exit_code == 0 and not vault.exists()
    assert not vault.parent.exists()  # the emptied per-source folder is removed too


def test_clean_yes_skips_the_prompt(runner: CliRunner, isolated_environment) -> None:
    _cache, vault = _seed_cache_and_vault(isolated_environment)
    assert runner.invoke(cli, ["clean", "--all", "--yes"]).exit_code == 0
    assert not vault.exists()


def test_clean_records_removes_upload_records_and_confirmations(
    runner: CliRunner, isolated_environment
) -> None:
    cache, vault = _seed_cache_and_vault(isolated_environment)
    sync_dir = isolated_environment.home / ".notebook-alchemy" / "sync"
    sync_dir.mkdir(parents=True)
    (sync_dir / "nb-1.json").write_text('{"titles": ["A"]}', encoding="utf-8")
    record_upload_consent(AccountIdentity("default", ACCOUNT))
    declined = runner.invoke(cli, ["clean", "--records"], input="n\n")
    assert declined.exit_code == 0 and "local records" in declined.output and consent_file().exists()
    assert runner.invoke(cli, ["clean", "--records", "--yes"]).exit_code == 0
    assert not list(sync_dir.iterdir()) and not consent_file().exists()
    assert list(cache.iterdir()) and vault.exists()  # --records touches nothing else


def test_clean_records_leaves_files_it_did_not_write(runner: CliRunner, isolated_environment) -> None:
    sync_dir = isolated_environment.home / ".notebook-alchemy" / "sync"
    sync_dir.mkdir(parents=True)
    (sync_dir / "mine.json").write_text('{"titles": ["A"]}', encoding="utf-8")
    (sync_dir / "theirs.json").write_text('{"settings": true}', encoding="utf-8")
    result = runner.invoke(cli, ["clean", "--records", "--yes"])
    assert result.exit_code == 0 and "theirs.json" in result.output
    assert [p.name for p in sync_dir.iterdir()] == ["theirs.json"]


def test_clean_reports_real_failures(
    runner: CliRunner, isolated_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cache_and_vault(isolated_environment)

    def locked(path, *a, **k):
        raise PermissionError("file is in use")

    monkeypatch.setattr("notebook_alchemy.cli.shutil.rmtree", locked)
    result = runner.invoke(cli, ["clean", "--vaults", "--yes"])
    assert result.exit_code == 1 and "could not be deleted" in result.output and "in use" in result.output


def test_clean_only_touches_this_tools_own_files(runner: CliRunner, isolated_environment) -> None:
    cache, _vault = _seed_cache_and_vault(isolated_environment)
    (cache / "notes.txt").write_text("not ours", encoding="utf-8")
    (cache / "other.json").write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    (cache / "sub").mkdir()
    (cache / "sub" / "deep.json").write_text(json.dumps({"video_id": "x"}), encoding="utf-8")
    result = runner.invoke(cli, ["clean"])
    assert result.exit_code == 0 and "Skipped" in result.output
    assert not (cache / "abc.json").exists()
    assert (cache / "notes.txt").exists() and (cache / "other.json").exists()
    assert (cache / "sub" / "deep.json").exists()  # never recurses into subfolders


def test_clean_asks_before_clearing_a_custom_cache_folder(
    runner: CliRunner, isolated_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = isolated_environment.work / "my-docs"
    custom.mkdir()
    (custom / "abc.json").write_text(json.dumps({"video_id": "abc"}), encoding="utf-8")
    monkeypatch.setenv("ALCHEMY_CACHE_DIR", str(custom))
    declined = runner.invoke(cli, ["clean"], input="n\n")
    assert "custom cache folder" in declined.output and (custom / "abc.json").exists()
    assert runner.invoke(cli, ["clean", "--yes"]).exit_code == 0
    assert not (custom / "abc.json").exists()


def test_clean_with_nothing_to_do(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["clean"])
    assert result.exit_code == 0 and "Nothing to clean" in result.output


# --- self-test ------------------------------------------------------------------------------------


def test_self_test_passes_offline_and_leaves_the_working_directory_untouched(
    runner: CliRunner, isolated_environment
) -> None:
    before = sorted(p.name for p in isolated_environment.work.iterdir())
    result = runner.invoke(cli, ["test"])
    assert result.exit_code == 0, result.output
    assert "All 7 checks passed" in result.output
    assert sorted(p.name for p in isolated_environment.work.iterdir()) == before
    assert not (isolated_environment.home / ".notebook-alchemy").exists()


@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        ([], None, logging.WARNING),
        ([], "DEBUG", logging.DEBUG),
        ([], "bogus", logging.WARNING),
        (["-v"], "ERROR", logging.INFO),
        (["-vv"], None, logging.DEBUG),
        (["-q"], "DEBUG", logging.ERROR),
    ],
)
def test_log_level_follows_flags_then_environment(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, args: list[str], env: str | None, expected: int
) -> None:
    if env:
        monkeypatch.setenv("ALCHEMY_LOG_LEVEL", env)
    root = logging.getLogger()
    before = root.level
    try:
        runner.invoke(cli, [*args, "clean"])
        assert root.level == expected
    finally:
        root.setLevel(before)


# --- upload account, profile and confirmation ------------------------------------------------------


def test_invalid_profile_option_is_a_config_error(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["status", "--profile", "../x"])
    assert result.exit_code == 2 and "profile name" in result.output


def test_status_shows_account_and_key_state_but_never_the_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    profile_dir = isolated_environment.home / ".notebooklm" / "profiles" / "personal"
    profile_dir.mkdir(parents=True)
    (profile_dir / "storage_state.json").write_text("{}", encoding="utf-8")
    key = "sk-ant-TEST-ONLY-not-a-real-key-000"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setattr(NotebookLMClient, "cli_version", lambda self: "0.8.6")
    result = runner.invoke(cli, ["status", "--profile", "personal"])
    assert result.exit_code == 0, result.output
    assert "[owner@example.test] (profile personal)" in result.output
    assert "AI sorting is on" in result.output
    assert "spend limit caps" not in result.output  # billing detail is shown with -v
    detailed = runner.invoke(cli, ["status", "--profile", "personal", "-v"])
    assert detailed.exit_code == 0 and "spend limit caps" in " ".join(detailed.output.split())
    for out in (result.output, detailed.output):
        assert key not in out and key[:10] not in out


# --- login --account --------------------------------------------------------------------------------


def test_login_window_with_account_checks_the_account_afterwards(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def ok(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", ok)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 2))
    wrong = runner.invoke(cli, ["login", "--account", "someone-else@example.test"])
    assert wrong.exit_code == 1 and "not someone-else@example.test" in wrong.output
    assert "--account" not in calls[0]  # a window sign-in is checked afterwards, not steered
    right = runner.invoke(cli, ["login", "--account", ACCOUNT])
    assert right.exit_code == 0, right.output


def test_login_rejects_an_unknown_cookie_browser(runner: CliRunner) -> None:
    assert runner.invoke(cli, ["login", "--browser-cookies", "netscape"]).exit_code == 2


def test_login_account_signs_in_that_profile_and_reads_it_back(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def ok(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", ok)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 2))
    monkeypatch.setattr(NotebookLMClient, "browser_accounts", lambda self, b: [ACCOUNT])
    args = ["login", "--browser-cookies", "Chrome::Profile 1", "--account", "Owner@Example.test"]
    result = runner.invoke(cli, [*args, "--profile", "personal"])
    assert result.exit_code == 0, result.output
    assert calls == [
        [
            "notebooklm",
            "--profile",
            "personal",
            "login",
            "--browser-cookies",
            "chrome::Profile 1",
            "--account",
            "Owner@Example.test",
        ]
    ]
    assert "owner@example.test" in result.output


def test_login_account_mismatch_is_an_error(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 2))
    args = ["login", "--browser-cookies", "edge", "--account", "someone-else@example.test"]
    result = runner.invoke(cli, args)
    assert result.exit_code == 1 and "not someone-else@example.test" in result.output


# --- setup ------------------------------------------------------------------------------------------


def test_setup_needs_a_terminal(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["setup"])
    assert result.exit_code == 2 and "terminal" in result.output


def test_setup_writes_env_without_echoing_the_key_and_reruns(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    env = isolated_environment.work / ".env"
    env.write_text("# keep me\nALCHEMY_LOG_LEVEL=INFO\n", encoding="utf-8")
    key = "sk-ant-TEST-ONLY-setup-key-0000000"
    # key, profile, sign-in choice, vault
    result = runner.invoke(cli, ["setup"], input=f"{key}\npersonal\nkeep\n")
    assert result.exit_code == 0, result.output
    assert key not in result.output and "Setup is complete" in result.output
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# keep me\nALCHEMY_LOG_LEVEL=INFO\n")
    assert f"ANTHROPIC_API_KEY={key}" in text and "ALCHEMY_NOTEBOOKLM_PROFILE=personal" in text
    assert "ALCHEMY_VAULT_PATH" not in text  # nothing is saved locally any more

    for name in ("ANTHROPIC_API_KEY", "ALCHEMY_NOTEBOOKLM_PROFILE"):
        os.environ.pop(name, None)  # loaded into this process by the first run
    rerun = runner.invoke(cli, ["setup"], input="\n\nkeep\n")  # Enter keeps everything
    assert rerun.exit_code == 0, rerun.output
    assert "A Claude API key is saved" in rerun.output and key not in rerun.output
    assert env.read_text(encoding="utf-8") == text


def test_setup_rejects_a_bad_key_and_signs_in_with_cookies(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setattr(NotebookLMClient, "browser_accounts", lambda self, b: [ACCOUNT])
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 0))
    calls: list[list[str]] = []

    def ok(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", ok)
    # bad key, then skip; default profile; cookies from edge; accept the only listed account; vault
    result = runner.invoke(cli, ["setup"], input="short\n\n\ncookies\nedge\n\n")
    assert result.exit_code == 0, result.output
    assert "does not look like an API key" in result.output
    assert calls == [
        ["notebooklm", "--profile", "default", "login", "--browser-cookies", "edge", "--account", ACCOUNT]
    ]
    text = (isolated_environment.work / ".env").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" not in text and "ALCHEMY_NOTEBOOKLM_PROFILE=default" in text


# --- review fixes ------------------------------------------------------------------------------------


def test_signed_in_email_is_printed_literally(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    odd = "[bold red]x[/bold red]@example.test"
    monkeypatch.setattr(NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, odd))
    monkeypatch.setattr(NotebookLMClient, "browser_accounts", lambda self, b: [odd])
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
    result = runner.invoke(cli, ["login", "--browser-cookies", "edge", "--account", odd])
    assert result.exit_code == 0, result.output
    assert f"You are now signed in as [{odd}]." in result.output


def test_login_refuses_an_account_the_browser_does_not_hold_before_signing_in(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(NotebookLMClient, "browser_accounts", lambda self, b: ["other@example.test"])
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: calls.append(cmd))
    result = runner.invoke(cli, ["login", "--browser-cookies", "edge", "--account", ACCOUNT])
    assert result.exit_code == 1 and "other@example.test" in result.output
    assert "Nothing was changed" in result.output and calls == []


def test_setup_warns_when_the_environment_overrides_what_it_writes(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setenv("ALCHEMY_NOTEBOOKLM_PROFILE", "work")
    result = runner.invoke(cli, ["setup"], input="\npersonal\nkeep\n")
    assert result.exit_code == 0, result.output
    assert "ALCHEMY_NOTEBOOKLM_PROFILE is also set in your environment" in result.output
    assert "ANTHROPIC_API_KEY is also set" not in result.output


def test_setup_repairs_a_bad_profile_in_env(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    env = isolated_environment.work / ".env"
    env.write_text("ALCHEMY_NOTEBOOKLM_PROFILE=bad/name\n", encoding="utf-8")
    assert runner.invoke(cli, ["status"]).exit_code == 2  # the bad value stops normal commands
    os.environ.pop("ALCHEMY_NOTEBOOKLM_PROFILE", None)
    result = runner.invoke(cli, ["setup"], input="\nfixed\nkeep\n")
    assert result.exit_code == 0, result.output
    assert "Setup will ask for a new one" in result.output
    assert "ALCHEMY_NOTEBOOKLM_PROFILE=fixed" in env.read_text(encoding="utf-8")
    assert "bad/name" not in env.read_text(encoding="utf-8")


def test_login_writes_the_session_file_every_other_call_reads(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    storage = isolated_environment.work / "my_session.json"
    storage.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ALCHEMY_SESSION_FILE", str(storage))
    calls: list[list[str]] = []

    def ok(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", ok)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 2))
    result = runner.invoke(cli, ["login", "--browser-cookies", "edge", "-v"])
    assert result.exit_code == 0, result.output
    assert calls[0][:3] == ["notebooklm", "--storage", str(storage)] and "login" in calls[0]
    assert "session file" in result.output  # shown with -v


def test_tabs_option_rejects_unknown_tabs(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["dry-run", "@my", "--tabs", "videos,reels"])
    assert result.exit_code == 2 and "shorts" in result.output


# --- quiet mode, caption notes and account replacement -----------------------------------------------


def test_dry_run_without_a_key_says_so(runner: CliRunner, fake_channel) -> None:
    fake_channel("Build a REST API", "CSS grid tricks")
    result = runner.invoke(cli, ["dry-run", "@my"])
    assert result.exit_code == 0 and "No Claude API key is set" in " ".join(result.output.split())


def test_login_asks_before_replacing_another_account(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def notebooklm(cmd, **k):
        calls.append(k)
        if k.get("input") == "y\n":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        question = (
            "profile 'default' already has auth for old@example.test. Overwrite it with owner@example.test?"
        )
        return subprocess.CompletedProcess(cmd, 1, question + "\nAborted!", "")

    monkeypatch.setattr(subprocess, "run", notebooklm)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    declined = runner.invoke(cli, ["login", "--browser-cookies", "edge"], input="n\n")
    assert declined.exit_code == 1 and "Nothing was changed" in declined.output and len(calls) == 1
    assert calls[0].get("stdin") is subprocess.DEVNULL  # notebooklm's own question cannot wait unseen
    calls.clear()
    accepted = runner.invoke(cli, ["login", "--browser-cookies", "edge", "--no-keep-signed-in"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert [c.get("input") for c in calls] == [None, "y\n"] and "signed in as" in accepted.output


def _signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))


def test_keep_signed_in_on_and_off(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(NotebookLMClient, "is_authenticated", lambda self: True)
    scheduler = isolated_environment.scheduler
    on = runner.invoke(cli, ["keep-signed-in", "-p", "personal"])
    assert on.exit_code == 0, on.output
    assert list(scheduler.tasks) == ["Notebook Alchemy keep signed in (personal)"]
    off = runner.invoke(cli, ["keep-signed-in", "-p", "personal", "--off"])
    assert off.exit_code == 0 and "is off" in off.output and not scheduler.tasks
    again = runner.invoke(cli, ["keep-signed-in", "-p", "personal", "--off"])
    assert "already off" in again.output


def test_keep_signed_in_needs_a_sign_in_first(runner: CliRunner, isolated_environment) -> None:
    result = runner.invoke(cli, ["keep-signed-in"])
    assert result.exit_code == 1 and "notebook-alchemy login" in result.output
    assert not isolated_environment.scheduler.tasks


# --- the book pipeline: fakes ------------------------------------------------------------------------

WORDS = (
    "Registering in SAM.gov takes about ten business days. You need your EIN exactly as the IRS letter "
    "shows it. Then request the CAGE code and add your bank account for payments."
)


@pytest.fixture
def fake_words(monkeypatch: pytest.MonkeyPatch):
    """Every video's words come back without the network."""

    def fetch_many(self, items, progress=None):
        return {
            vid: TranscriptResult(vid, f"{WORDS} This video is {title}.", "youtube") for vid, title in items
        }

    monkeypatch.setattr(TranscriptManager, "fetch_many", fetch_many)


def _record_uploads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    uploads: list[str] = []

    def fake_publish(self, title, books, **kw):
        uploads.append(title)
        return SyncResult("nb-1", True, len(books), 0)

    monkeypatch.setattr(AlchemyEngine, "publish", fake_publish)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))
    return uploads


RUN = ["run", "@my", "--offline"]


# --- run, dry-run and the removed commands --------------------------------------------------------------


def test_dry_run_mock_is_labelled_sample(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["dry-run", "--mock"])
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "sample data" in flat and "36 videos" in flat and "free without AI" in flat
    assert "To build the books" not in flat  # nothing to run next on sample data


def test_run_mock_saves_books_and_never_uploads(runner: CliRunner, isolated_environment) -> None:
    refused = runner.invoke(cli, ["run", "--mock"])
    assert refused.exit_code == 2 and "--save" in refused.output
    result = runner.invoke(cli, ["run", "--mock", "--save", "books"])
    assert result.exit_code == 0, result.output
    saved = sorted(p.name for p in (isolated_environment.work / "books").iterdir())
    assert "_OVERVIEW.md" in saved and len(saved) >= 2
    assert "never uploaded" in " ".join(result.output.split())


def test_run_without_a_session_stops_before_any_work(
    runner: CliRunner, fake_channel, isolated_environment
) -> None:
    fake_channel("a", "b")
    result = runner.invoke(cli, ["run", "@my"])
    assert result.exit_code == 1
    assert "notebook-alchemy login" in result.output and "--save" in result.output


def test_run_save_writes_books_with_titles_printed_exactly(
    runner: CliRunner, fake_channel, fake_words, isolated_environment
) -> None:
    fake_channel("CSS [live] tricks", "REST [/start] API", "AI [4K] demo", "日本語")
    result = runner.invoke(cli, [*RUN, "--save", "books"])
    assert result.exit_code == 0, result.output
    text = " ".join(p.read_text(encoding="utf-8") for p in (isolated_environment.work / "books").iterdir())
    for title in ("[live]", "[/start]", "[4K]", "日本語"):
        assert title in text
    assert not text.startswith("---")


def test_run_uploads_and_reports(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("Build a REST API", "CSS grid tricks")
    uploads = _record_uploads(monkeypatch)
    result = runner.invoke(cli, [*RUN, "-N", "My Notebook", "--yes"])
    assert result.exit_code == 0, result.output
    assert uploads == ["My Notebook"]
    assert "Created notebook [My Notebook]." in result.output and "notebooklm.google.com" in result.output


def test_a_rerun_with_nothing_changed_says_the_notebook_is_up_to_date(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("Build a REST API")
    monkeypatch.setattr(
        AlchemyEngine,
        "publish",
        lambda self, title, books, **kw: SyncResult("nb-1", False, 0, 0, unchanged=len(books)),
    )
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))
    result = runner.invoke(cli, [*RUN, "-N", "My Notebook", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Notebook [My Notebook] is already up to date." in result.output
    assert "Updated notebook" not in result.output


def test_an_upload_failure_exits_non_zero_and_lists_each_book(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a", "b")
    _record_uploads(monkeypatch)

    def boom(self, title, books, **kw):
        raise SyncError(
            "1 of 2 books did not upload.", failed=["Pricing: No such option"], hint="Run it again."
        )

    monkeypatch.setattr(AlchemyEngine, "publish", boom)
    result = runner.invoke(cli, [*RUN, "-N", "X", "--yes"])
    assert (
        result.exit_code == 1
        and "Pricing: No such option" in result.output
        and "Run it again" in result.output
    )


@pytest.mark.parametrize(
    ("args", "pointer"),
    [
        (["sync", "-n", "chan"], "`sync` was removed"),
        (["append", "--title", "t", "--url", "u"], "`append` was removed"),
        (["run", "@my", "--no-sync"], "--save DIR"),
        (["run", "@my", "--max-per-vol", "5"], "sized automatically"),
        (["run", "@my", "--preview-only"], "dry-run"),
    ],
)
def test_removed_commands_and_flags_say_what_replaced_them(
    runner: CliRunner, args: list[str], pointer: str
) -> None:
    result = runner.invoke(cli, args)
    assert result.exit_code == 2 and pointer in " ".join(result.output.split())


def test_quiet_hides_progress_chatter(runner: CliRunner, fake_channel, fake_words) -> None:
    fake_channel("a b c d", "e f g h")
    loud = runner.invoke(cli, [*RUN, "--save", "b1"]).output
    quiet = runner.invoke(cli, ["-q", *RUN, "--save", "b2"]).output
    assert "Step 1 of 2" in loud and "Step 1 of 2" not in quiet


def test_quiet_still_prints_the_result_and_the_sample_label(runner: CliRunner, isolated_environment) -> None:
    result = runner.invoke(cli, ["-q", "run", "--mock", "--save", "books"])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "Saved" in flat and "sample data" in flat and "Step 1" not in flat


def test_build_notices_are_shown_without_v(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    real = AlchemyEngine.build

    def with_notice(self, *a, **kw):
        report = real(self, *a, **kw)
        report.notices.append(
            "The Claude API rate limit is still reached after waiting. Some videos wait for the next run."
        )
        return report

    monkeypatch.setattr(AlchemyEngine, "build", with_notice)
    flat = " ".join(runner.invoke(cli, [*RUN, "--save", "b"]).output.split())
    assert "Warning: The Claude API rate limit is still reached" in flat


def test_clean_ignores_folders_that_are_not_old_vault_output(runner: CliRunner, isolated_environment) -> None:
    stray = isolated_environment.work / "vault" / "mine" / "compendiums"
    stray.mkdir(parents=True)
    (stray / "important.md").write_text("hand-written", encoding="utf-8")
    result = runner.invoke(cli, ["clean", "--vaults", "--yes"])
    assert "Nothing to clean" in result.output and (stray / "important.md").exists()


def test_clean_vaults_removes_old_version_output(runner: CliRunner, isolated_environment) -> None:
    old = isolated_environment.work / "vault" / "chan" / "compendiums"
    old.mkdir(parents=True)
    (old / "_INDEX.md").write_text("index", encoding="utf-8")
    result = runner.invoke(cli, ["clean", "--vaults", "--yes"])
    assert result.exit_code == 0 and not old.exists()


def test_setup_asks_for_no_folder(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, ["setup"], input="\ndefault\nkeep\n")
    assert result.exit_code == 0, result.output
    assert "Step 2 of 2" in result.output and "folder" not in result.output.lower()


# --- upload account, profile and confirmation ------------------------------------------------------------


def test_profile_option_and_env_select_the_profile_for_every_notebooklm_call(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    fake_channel("a b c d")
    profile_dir = isolated_environment.home / ".notebooklm" / "profiles" / "work"
    profile_dir.mkdir(parents=True)
    (profile_dir / "storage_state.json").write_text("{}", encoding="utf-8")
    fake = FakeNotebookLM()
    monkeypatch.setattr(subprocess, "run", fake)
    result = runner.invoke(cli, [*RUN, "-N", "chan", "--profile", "work", "--yes"])
    assert result.exit_code == 0, result.output
    assert fake.calls and all(c[1:3] == ["--profile", "work"] for c in fake.calls)
    assert "Uploads go to [owner@example.test] (profile work)" in result.output
    fake.calls.clear()
    monkeypatch.setenv("ALCHEMY_NOTEBOOKLM_PROFILE", "work")
    assert runner.invoke(cli, [*RUN, "-N", "chan", "--yes"]).exit_code == 0
    assert fake.calls and all(c[1:3] == ["--profile", "work"] for c in fake.calls)


def test_unknown_upload_account_is_shown_as_unknown(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    monkeypatch.setattr(
        NotebookLMClient,
        "upload_account",
        lambda self: AccountIdentity(self.profile, None, "the sign-in did not record an account"),
    )
    uploads = _record_uploads(monkeypatch)
    result = runner.invoke(cli, [*RUN, "-N", "chan", "--yes"])
    assert result.exit_code == 0, result.output
    shown = result.output.split("Uploads go to")[1].split("\n")[0]
    assert "profile [default]" in shown and "@" not in shown  # said plainly, never guessed
    flat = " ".join(result.output.split())
    assert "did not record" in flat and "--account <email>" in flat and uploads == ["chan"]


def test_first_upload_without_a_terminal_needs_yes(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    result = runner.invoke(cli, [*RUN, "-N", "chan"])
    assert result.exit_code == 1 and "first upload" in result.output and "--yes" in result.output
    assert uploads == []
    assert runner.invoke(cli, [*RUN, "-N", "chan", "--yes"]).exit_code == 0 and uploads == ["chan"]
    assert not upload_confirmed(AccountIdentity("default", ACCOUNT))  # --yes skips; it does not consent


def test_first_upload_asks_once_per_account(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    declined = runner.invoke(cli, [*RUN, "-N", "chan"], input="n\n")
    assert declined.exit_code == 1 and "Nothing was uploaded" in declined.output and uploads == []
    assert "Do you want to continue (y/N)?" in declined.output and "owner@example.test" in declined.output
    accepted = runner.invoke(cli, [*RUN, "-N", "chan"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    again = runner.invoke(cli, [*RUN, "-N", "chan"])  # no input needed the second time
    assert again.exit_code == 0 and "Do you want to continue" not in again.output
    monkeypatch.setattr(
        NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, "new@example.test")
    )
    changed = runner.invoke(cli, [*RUN, "-N", "chan"], input="n\n")
    assert changed.exit_code == 1 and "Do you want to continue" in changed.output
    assert uploads == ["chan", "chan"]


def test_an_unknown_account_is_asked_about_every_time(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    monkeypatch.setattr(NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, None))
    uploads = _record_uploads(monkeypatch)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    assert runner.invoke(cli, [*RUN, "-N", "chan"], input="y\n").exit_code == 0
    second = runner.invoke(cli, [*RUN, "-N", "chan"], input="n\n")
    assert second.exit_code == 1 and "Do you want to continue" in second.output and uploads == ["chan"]


def test_quiet_first_upload_question_still_names_the_account(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    _record_uploads(monkeypatch)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, ["-q", *RUN, "-N", "chan"], input="n\n")
    assert "Notebooks will be created in [owner@example.test]." in result.output


def _session_sequence(monkeypatch: pytest.MonkeyPatch, *states: bool) -> list[str]:
    """check_session answers these states in turn (then the last one); returns the sign-ins run."""
    answers = iter(states)
    last = [states[-1]]

    def check(self):
        ok = next(answers, last[0])
        return SessionStatus(ok, "ok" if ok else "Authentication expired or invalid.", 1, signed_out=not ok)

    monkeypatch.setattr(NotebookLMClient, "check_session", check)
    monkeypatch.setattr(NotebookLMClient, "is_authenticated", lambda self: True)
    signed: list[str] = []

    def sign_in(client, **kw):
        signed.append(kw.get("account") or client.profile)

    monkeypatch.setattr(cli_module, "_sign_in", sign_in)
    return signed


def test_an_ended_sign_in_outside_a_terminal_stops_with_the_sign_in_command(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    signed = _session_sequence(monkeypatch, False)
    result = runner.invoke(cli, [*RUN, "-N", "chan"])
    assert result.exit_code == 1 and "Authentication expired" in result.output
    assert "notebook-alchemy login" in result.output and uploads == [] and signed == []


def test_an_ended_sign_in_in_a_terminal_is_offered_and_the_run_carries_on(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    signed = _session_sequence(monkeypatch, False, True)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    # Enter: sign in now; y: the first upload to this account
    result = runner.invoke(cli, [*RUN, "-N", "chan"], input="\ny\n")
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "Your NotebookLM sign-in has ended" in flat and "Do you want to sign in now (Y/n)?" in flat
    assert signed == ["default"] and uploads == ["chan"]


def test_with_yes_an_ended_sign_in_goes_straight_to_signing_in(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    signed = _session_sequence(monkeypatch, False, True)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, [*RUN, "-N", "chan", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Do you want to sign in now" not in result.output and signed == ["default"] and uploads == ["chan"]


def test_a_network_fault_is_not_mistaken_for_an_ended_sign_in(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    signed = _session_sequence(monkeypatch, True)
    monkeypatch.setattr(
        NotebookLMClient, "check_session", lambda self: SessionStatus(False, "`notebooklm list` timed out.")
    )
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, [*RUN, "-N", "chan", "--yes"])
    assert result.exit_code == 1 and "could not be reached" in result.output
    assert "sign in now" not in result.output and signed == [] and uploads == []


def test_save_never_asks_for_a_sign_in(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    fake_channel("a b c d")
    signed = _session_sequence(monkeypatch, False)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, [*RUN, "--save", "books"])
    assert result.exit_code == 0, result.output and signed == []


def test_declining_the_sign_in_uploads_nothing(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    signed = _session_sequence(monkeypatch, False)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, [*RUN, "-N", "chan"], input="n\n")
    assert result.exit_code == 1 and "Nothing was uploaded" in result.output
    assert uploads == [] and signed == []


def test_a_sign_in_that_ends_during_the_build_is_offered_again_before_uploading(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    signed = _session_sequence(monkeypatch, True, False, True)  # fine at the start, ended at upload
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, [*RUN, "-N", "chan", "--yes"])
    assert result.exit_code == 0, result.output
    assert "before uploading" in " ".join(result.output.split())
    assert signed == [ACCOUNT] and uploads == ["chan"]  # signed in again to the confirmed account


def test_an_unrecorded_account_is_asked_about_again_after_signing_in_again(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    _session_sequence(monkeypatch, True, False, True)
    monkeypatch.setattr(NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, None))
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    # y: upload to the unknown account; Enter: sign in again; n: not to the account signed in now
    result = runner.invoke(cli, [*RUN, "-N", "chan"], input="y\n\nn\n")
    assert result.exit_code == 1 and "After signing in again, uploads go to" in " ".join(
        result.output.split()
    )
    assert uploads == []


def test_signing_in_again_to_another_account_uploads_nothing(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    _session_sequence(monkeypatch, True, False, True)
    accounts = iter([ACCOUNT, "other@example.test"])
    monkeypatch.setattr(
        NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, next(accounts))
    )
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, [*RUN, "-N", "chan", "--yes"])
    assert result.exit_code == 1 and "upload account changed" in " ".join(result.output.split())
    assert uploads == []


def test_login_asks_nothing_about_the_background_refresh(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _signed_in(monkeypatch)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, ["login", "--browser-cookies", "edge"])
    assert result.exit_code == 0, result.output
    assert "keep this sign-in fresh" not in result.output and not isolated_environment.scheduler.tasks
    opted = runner.invoke(cli, ["login", "--browser-cookies", "edge", "--keep-signed-in"])
    assert opted.exit_code == 0 and list(isolated_environment.scheduler.tasks) == [
        "Notebook Alchemy keep signed in (default)"
    ]


def test_status_says_nothing_about_a_background_refresh_that_is_off(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(NotebookLMClient, "session_file", lambda self: Path("storage_state.json"))
    result = runner.invoke(cli, ["status"])
    assert "background" not in result.output
    from notebook_alchemy.cli import _keepalive_line
    from notebook_alchemy.keepalive import KeepAliveState

    assert _keepalive_line(KeepAliveState(False, "")) is None


def test_run_stops_if_the_account_changes_during_the_build(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    seen = iter([ACCOUNT, "other@example.test"])
    monkeypatch.setattr(
        NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, next(seen))
    )
    result = runner.invoke(cli, [*RUN, "-N", "My Notebook", "--yes"])
    assert result.exit_code == 1 and "changed while the documents were being written" in result.output
    assert "other@example.test" in result.output and uploads == []


def test_run_shows_the_plan_and_the_upload_stops_when_the_books_cannot_fit(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    fake_channel("Pricing a bid well", "SAM.gov registration")
    profile_dir = isolated_environment.home / ".notebooklm" / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "storage_state.json").write_text("{}", encoding="utf-8")
    fake = FakeNotebookLM()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 3))
    monkeypatch.setattr(NotebookLMClient, "plan_limits", lambda self: PlanLimits(1, 100, 1))
    result = runner.invoke(cli, [*RUN, "-N", "Mine", "--yes"])
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert "Standard, up to 1 sources per notebook" in flat and "Nothing was uploaded" in flat
    assert not any("add" in c for c in fake.calls if "source" in c)


def test_run_says_when_the_plan_is_unknown(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    result = runner.invoke(cli, [*RUN, "-N", "Mine", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Uploads go to [owner@example.test]." in result.output and uploads == ["Mine"]
    detailed = runner.invoke(cli, [*RUN, "-N", "Mine", "--yes", "-v"])
    assert "NotebookLM plan: unknown" in " ".join(detailed.output.split())  # never guessed


def test_nothing_is_uploaded_while_videos_wait_for_labels(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c d")
    uploads = _record_uploads(monkeypatch)
    real = AlchemyEngine.build

    def waiting(self, *a, **kw):
        report = real(self, *a, **kw)
        report.waiting = ["vid00000000"]
        return report

    monkeypatch.setattr(AlchemyEngine, "build", waiting)
    result = runner.invoke(cli, ["-q", *RUN, "-N", "chan", "--yes"])
    assert result.exit_code == 0 and uploads == []
    assert "still waits for labels, so no books were written and nothing was uploaded" in " ".join(
        result.output.split()
    )  # even with -q


@pytest.mark.parametrize("flag", [["-n", "x"], ["--sync"], ["--write"]])
def test_the_other_removed_run_flags_say_what_replaced_them(runner: CliRunner, flag: list[str]) -> None:
    result = runner.invoke(cli, ["run", "@my", *flag])
    assert result.exit_code == 2 and "was removed" in result.output


def test_clean_records_keeps_a_running_batch_and_clean_labels_forgets_it(
    runner: CliRunner, isolated_environment
) -> None:
    home = isolated_environment.home / ".notebook-alchemy"
    (home / "batches").mkdir(parents=True, exist_ok=True)
    batch = home / "batches" / "Chan.json"
    batch.write_text('{"job": "msgbatch_test"}', encoding="utf-8")
    (home / "notebooks.json").write_text('{"@chan": "nb-1"}', encoding="utf-8")
    assert runner.invoke(cli, ["clean", "--records", "--yes"]).exit_code == 0
    assert batch.exists() and not (home / "notebooks.json").exists()
    assert runner.invoke(cli, ["clean", "--labels", "--yes"]).exit_code == 0
    assert not batch.exists()


def test_setup_without_a_sign_in_says_so(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, ["setup"], input="\ndefault\nkeep\n")
    assert result.exit_code == 0, result.output
    assert "Not signed in to NotebookLM yet" in result.output and "Uploads go to" not in result.output


# --- the guided first run ---------------------------------------------------------------------------


def test_no_command_outside_a_terminal_prints_the_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, [])
    assert result.exit_code == 0 and "Usage:" in result.output and "guided first run" in result.output


def _recorded_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    runs: list[dict[str, object]] = []
    fake = click.Command(
        "run",
        callback=lambda **kw: runs.append(kw),
        params=[click.Argument(["source"]), click.Option(["--offline"], is_flag=True)],
    )
    monkeypatch.setattr(cli_module, "run_pipeline", fake)
    return runs


def _guided(monkeypatch: pytest.MonkeyPatch, *, signed_in: bool) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setattr(NotebookLMClient, "is_authenticated", lambda self: signed_in)
    monkeypatch.setattr(
        NotebookLMClient,
        "check_session",
        lambda self: SessionStatus(signed_in, "ok", 1, signed_out=not signed_in),
    )


def test_the_guided_run_needs_only_a_channel_link_when_signed_in(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _guided(monkeypatch, signed_in=True)
    runs = _recorded_run(monkeypatch)
    # key: Enter (no); a wrong link, then a channel
    result = runner.invoke(cli, [], input="\nnot a link\n@example-channel\n")
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert f"Signed in as [{ACCOUNT}]" in flat and "does not look like a YouTube link" in flat
    assert runs == [{"source": "@example-channel", "offline": False, "tabs": ("videos", "streams")}]
    assert "notebook-alchemy run @example-channel" in flat
    assert "keep this sign-in fresh" not in flat and not isolated_environment.scheduler.tasks


def test_the_guided_run_can_add_a_claude_api_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _guided(monkeypatch, signed_in=True)
    runs = _recorded_run(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(click, "launch", lambda url, **kw: opened.append(url))
    key = "sk-ant-TEST-ONLY-not-a-real-key-001"
    result = runner.invoke(cli, [], input=f"y\n{key}\nhttps://www.youtube.com/@example-channel\n")
    assert result.exit_code == 0, result.output
    assert opened == ["https://platform.claude.com/settings/keys"] and key not in result.output
    assert f"ANTHROPIC_API_KEY={key}" in (isolated_environment.work / ".env").read_text(encoding="utf-8")
    assert runs == [
        {
            "source": "https://www.youtube.com/@example-channel",
            "offline": False,
            "tabs": ("videos", "streams"),
        }
    ]


def test_the_guided_run_stops_cleanly_without_a_sign_in(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _guided(monkeypatch, signed_in=False)
    runs = _recorded_run(monkeypatch)
    result = runner.invoke(cli, [], input="n\n")
    assert result.exit_code == 1 and "no NotebookLM sign-in" in result.output and runs == []


def test_the_guided_run_builds_and_uploads_for_real(
    runner: CliRunner, fake_channel, fake_words, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _guided(monkeypatch, signed_in=True)
    fake_channel("Build a REST API", "CSS grid tricks")
    uploads = _record_uploads(monkeypatch)
    # n: no Claude API key; the channel; y: the upload account; Enter: the notebook name
    result = runner.invoke(cli, [], input="n\n@my\ny\n\n")
    assert result.exit_code == 0, result.output
    assert len(uploads) == 1 and "Created notebook" in result.output
    assert "does not look like a YouTube link" not in result.output  # every answer went where meant


def test_keep_signed_in_is_refused_with_a_session_file(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment, tmp_path: Path
) -> None:
    session = tmp_path / "storage_state.json"
    session.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ALCHEMY_SESSION_FILE", str(session))
    monkeypatch.setattr(NotebookLMClient, "is_authenticated", lambda self: True)
    result = runner.invoke(cli, ["keep-signed-in"])
    assert result.exit_code != 0 and "ALCHEMY_SESSION_FILE" in result.output
    assert not isolated_environment.scheduler.tasks


def test_the_guided_run_warns_when_the_environment_key_would_win(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _guided(monkeypatch, signed_in=True)
    _recorded_run(monkeypatch)
    monkeypatch.setattr(click, "launch", lambda url, **kw: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "your_key_here")  # set, but not a usable key
    key = "sk-ant-TEST-ONLY-not-a-real-key-001"
    result = runner.invoke(cli, [], input=f"y\n{key}\n@example-channel\n")
    assert result.exit_code == 0, result.output
    assert "also set in your environment" in " ".join(result.output.split())


def _login_runs(monkeypatch: pytest.MonkeyPatch, *checks: bool) -> list[list[str]]:
    """`notebooklm login` always succeeds; check_session answers `checks` in turn."""
    logins: list[list[str]] = []

    def run(cmd, **kw):
        logins.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    answers = iter(checks)

    def check(self):
        ok = next(answers, checks[-1])
        return SessionStatus(ok, "ok" if ok else "Authentication expired or invalid.", 1, signed_out=not ok)

    monkeypatch.setattr(NotebookLMClient, "check_session", check)
    return logins


def test_a_sign_in_that_reused_a_stale_browser_opens_a_clean_window(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    logins = _login_runs(monkeypatch, False, True)  # the reused browser did not work; the clean one did
    result = runner.invoke(cli, ["login"])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert f"Sign in as [{ACCOUNT}]" in flat and "https://notebooklm.google.com" in flat
    assert "a clean sign-in window opens" in flat and "click it to reach Google's sign-in page" in flat
    assert ["--fresh" in cmd for cmd in logins] == [False, True]


def test_a_clean_window_that_still_fails_is_reported_not_retried_again(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    logins = _login_runs(monkeypatch, False)
    result = runner.invoke(cli, ["login"])
    assert result.exit_code == 1 and "the check that it works failed" in result.output
    assert len(logins) == 2  # the first sign-in and one clean retry


def test_watch_says_it_was_removed(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["run", "@my", "--watch", "3"])
    assert result.exit_code == 2 and "--watch was removed" in " ".join(result.output.split())


def test_an_old_gemini_key_alone_stops_the_run_before_it_replaces_sorted_books(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "TEST-ONLY-old-gemini-key-0000")
    result = runner.invoke(cli, ["run", "@my", "--save", "b"])
    flat = " ".join(result.output.split())
    assert result.exit_code != 0 and "now uses Claude" in flat and "notebook-alchemy setup" in flat
    assert runner.invoke(cli, ["run", "--mock", "--save", "b"]).exit_code == 0  # sample data needs no key


def test_dry_run_warns_when_the_model_has_no_listed_price(
    runner: CliRunner, fake_channel, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("a b c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-TEST-ONLY-not-a-real-key-002")
    monkeypatch.setenv("ALCHEMY_MODEL", "claude-unlisted-test")
    result = runner.invoke(cli, ["dry-run", "@my"])
    assert result.exit_code == 0, result.output
    assert "No listed price for 'claude-unlisted-test'" in " ".join(result.output.split())


def test_the_guided_run_hands_the_real_run_command_a_tuple_of_tabs(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _guided(monkeypatch, signed_in=True)
    seen: dict[str, object] = {}

    def scan(self, url, limit=None, tabs=()):
        seen["tabs"] = tabs
        raise AlchemyError("stop here")

    from notebook_alchemy.channel import ChannelScanner

    monkeypatch.setattr(ChannelScanner, "scan", scan)
    monkeypatch.setattr(cli_module, "_show_upload_target", lambda engine: None)
    monkeypatch.setattr(cli_module, "_confirm_first_upload", lambda confirmed, yes: None)
    runner.invoke(cli, [], input="\n@example-channel\n")
    assert seen["tabs"] == ("videos", "streams")


def test_no_books_are_written_while_videos_wait_for_labels(
    runner: CliRunner, fake_channel, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_channel("a b c")
    real = AlchemyEngine.build

    def waiting(self, *a, **kw):
        report = real(self, *a, **kw)
        report.waiting = [report.videos[0].id]
        return report

    monkeypatch.setattr(AlchemyEngine, "build", waiting)
    out = tmp_path / "books"
    result = runner.invoke(cli, [*RUN, "--save", str(out)])
    flat = " ".join(result.output.split())
    assert "still waits for labels, so no books were written" in flat and not out.exists()


def test_clean_leaves_files_that_are_not_shaped_like_its_own(runner: CliRunner, isolated_environment) -> None:
    home = isolated_environment.home / ".notebook-alchemy"
    (home / "batches").mkdir(parents=True, exist_ok=True)
    (home / "library").mkdir(parents=True, exist_ok=True)
    mine = home / "batches" / "Chan.json"
    mine.write_text('{"job": "msgbatch_test", "keys": []}', encoding="utf-8")
    other = home / "batches" / "notes.json"
    other.write_text('{"shopping": ["milk"]}', encoding="utf-8")
    library_other = home / "library" / "budget.json"
    library_other.write_text('{"rent": 1200}', encoding="utf-8")
    notebooks = home / "notebooks.json"
    notebooks.write_text('{"settings": {"theme": "dark"}}', encoding="utf-8")
    result = runner.invoke(cli, ["clean", "--labels", "--records", "--yes"])
    assert result.exit_code == 0, result.output
    assert not mine.exists() and other.exists() and library_other.exists() and notebooks.exists()
    assert "does not look like Notebook Alchemy output" in " ".join(result.output.split())


def test_an_unreadable_refresh_record_is_reported_as_unknown(isolated_environment) -> None:
    from notebook_alchemy.cli import _keepalive_line
    from notebook_alchemy.keepalive import KeepAliveState, status_problem

    home = isolated_environment.home / ".notebook-alchemy"
    home.mkdir(parents=True, exist_ok=True)
    (home / "keepalive.json").write_text("{not json", encoding="utf-8")
    problem = status_problem(home)
    assert problem
    ok, text = _keepalive_line(KeepAliveState(True, "Task Scheduler", unreadable=problem))
    assert ok is False and "could not be read" in text


def test_export_warns_when_a_labelling_batch_is_still_running(
    runner: CliRunner, isolated_environment, tmp_path: Path
) -> None:
    home = isolated_environment.home / ".notebook-alchemy"
    (home / "batches").mkdir(parents=True, exist_ok=True)
    (home / "batches" / "Chan.json").write_text('{"job": "msgbatch_test"}', encoding="utf-8")
    result = runner.invoke(cli, ["export-state", str(tmp_path / "state.zip")])
    assert result.exit_code == 0, result.output
    assert "still running here and is not in the file" in " ".join(result.output.split())


def test_a_saved_build_says_whose_notebooklm_reads_blocked_videos(
    runner: CliRunner, fake_channel, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_channel("a b c")
    monkeypatch.setattr(NotebookLMClient, "is_authenticated", lambda self: True)
    result = runner.invoke(cli, [*RUN, "--save", str(tmp_path / "books")])
    assert "read through NotebookLM as" in " ".join(result.output.split())
