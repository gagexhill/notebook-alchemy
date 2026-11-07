"""Command line behaviour: output, exit codes, safety."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from notebook_alchemy import __version__, ui
from notebook_alchemy import cli as cli_module
from notebook_alchemy.channel import ChannelScanner, ChannelScanResult, ChannelVideo
from notebook_alchemy.cli import cli
from notebook_alchemy.client import (
    AccountIdentity,
    NotebookLMClient,
    SessionStatus,
    consent_file,
    record_upload_consent,
    upload_confirmed,
)
from notebook_alchemy.engine import AlchemyEngine, SyncResult
from notebook_alchemy.errors import ChannelScanError, SyncError

from .conftest import FakeNotebookLM


@pytest.fixture(autouse=True)
def reset_ui() -> None:
    yield
    ui.console = Console(highlight=False)
    ui.err_console = Console(stderr=True, highlight=False)
    ui.UiState.quiet = ui.UiState.debug = False


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
        out.index(f"\n  {name}")
        for name in ("run", "dry-run", "status", "login", "sync", "append", "clean", "test")
    ]
    assert order == sorted(order)
    assert "First run:" in out


@pytest.mark.parametrize("command", ["run", "dry-run", "login", "setup", "sync", "append", "clean"])
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


def test_dry_run_mock_is_labelled_sample(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["dry-run", "--mock", "--offline"])
    assert result.exit_code == 0
    assert "Sample data" in result.output and "Estimated total" in result.output
    assert "Next:" not in result.output  # nothing to run next on sample data


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


def test_bracketed_titles_print_exactly_and_do_not_crash(runner: CliRunner, fake_channel) -> None:
    # one title per domain so each shows in the Examples column
    fake_channel("CSS [live] tricks", "REST [/start] API", "AI [4K] demo", "日本語")
    result = runner.invoke(cli, ["dry-run", "@my", "--offline"])
    assert result.exit_code == 0, result.output
    for title in ("[live]", "[/start]", "[4K]", "日本語"):
        assert title in result.output


# --- run ------------------------------------------------------------------------------------------


def test_run_mock_builds_in_the_vault_and_never_uploads(runner: CliRunner, isolated_environment) -> None:
    result = runner.invoke(cli, ["run", "--mock", "--offline"])
    assert result.exit_code == 0, result.output
    assert "never uploaded" in result.output and "Build complete" in result.output
    assert (
        isolated_environment.work / "vault" / "sample_catalog_mock_data" / "compendiums" / "_INDEX.md"
    ).exists()


def test_run_without_a_session_stops_before_any_work(
    runner: CliRunner, fake_channel, isolated_environment
) -> None:
    fake_channel("a", "b")
    result = runner.invoke(cli, ["run", "@my"])
    assert result.exit_code == 1
    assert "notebook-alchemy login" in result.output and "--no-sync" in result.output
    assert not (isolated_environment.work / "vault").exists()


def test_run_no_sync_builds_for_real_sources(runner: CliRunner, fake_channel, isolated_environment) -> None:
    fake_channel("Build a REST API", "CSS grid tricks", "AI agents explained")
    result = runner.invoke(cli, ["run", "@my", "--no-sync", "--offline", "-y"])
    assert result.exit_code == 0, result.output
    assert (isolated_environment.work / "vault" / "my_channel" / "compendiums" / "_INDEX.md").exists()
    assert "notebook-alchemy sync -n" in result.output


def test_run_with_sync_uploads_and_reports(
    runner: CliRunner, fake_channel, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("Build a REST API", "CSS grid tricks")
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 3))
    seen: dict[str, object] = {}

    def fake_sync(self, title, source_dir, progress=None):
        seen["title"], seen["dir"] = title, source_dir
        return SyncResult("nb-9", True, 4, 0)

    monkeypatch.setattr(AlchemyEngine, "sync_to_notebooklm", fake_sync)
    result = runner.invoke(cli, ["run", "@my", "--offline", "-N", "My Notebook", "--yes"])
    assert result.exit_code == 0, result.output
    assert seen["title"] == "My Notebook" and Path(str(seen["dir"])).name == "compendiums"
    assert "Upload complete" in result.output and "Created notebook" in result.output


def test_sync_failure_exits_non_zero_and_lists_files(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    d = isolated_environment.work / "vault" / "chan" / "compendiums"
    d.mkdir(parents=True)

    def boom(self, title, source_dir, progress=None):
        raise SyncError(
            "1 of 2 files did not upload.", failed=["Beta.md: No such option"], hint="Run it again."
        )

    monkeypatch.setattr(AlchemyEngine, "sync_to_notebooklm", boom)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))
    result = runner.invoke(cli, ["sync", "-n", "chan", "--yes"])
    assert result.exit_code == 1
    assert "Beta.md: No such option" in result.output and "Run it again" in result.output


def test_sync_unknown_source_lists_what_exists(runner: CliRunner, isolated_environment) -> None:
    (isolated_environment.work / "vault" / "chan" / "compendiums").mkdir(parents=True)
    result = runner.invoke(cli, ["sync", "-n", "nothing"])
    assert result.exit_code == 1 and "chan" in result.output


def test_unexpected_errors_are_contained(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self, *a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ChannelScanner, "scan", boom)
    plain = runner.invoke(cli, ["dry-run", "@x"])
    assert plain.exit_code == 70 and "Unexpected error: RuntimeError: kaboom" in plain.output
    assert "Traceback" not in plain.output
    debug = runner.invoke(cli, ["--debug", "dry-run", "@x"])
    assert "Traceback" in debug.output


def test_quiet_hides_progress_chatter(runner: CliRunner, fake_channel) -> None:
    fake_channel("a", "b")
    loud = runner.invoke(cli, ["run", "@my", "--no-sync", "--offline"]).output
    quiet = runner.invoke(cli, ["-q", "run", "@my", "--no-sync", "--offline"]).output
    assert "[1/2]" in loud and "[1/2]" not in quiet


# --- status / login -------------------------------------------------------------------------------


def test_status_is_offline_and_actionable(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NotebookLMClient, "cli_version", lambda self: None)
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "pip install notebooklm-py" in result.output and "notebook-alchemy login" in result.output
    assert "GEMINI_API_KEY" in result.output


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


def test_setup_saves_a_home_relative_vault_path_without_the_home_folder(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, ["setup"], input="\ndefault\nkeep\n~/alchemy-vault\n")
    assert result.exit_code == 0, result.output
    text = (isolated_environment.work / ".env").read_text(encoding="utf-8")
    assert "ALCHEMY_VAULT_PATH=~/alchemy-vault" in text
    assert str(isolated_environment.home) not in text


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


def test_clean_skips_vault_folders_without_marker_files(runner: CliRunner, isolated_environment) -> None:
    stray = isolated_environment.work / "vault" / "mine" / "compendiums"
    stray.mkdir(parents=True)
    (stray / "important.md").write_text("hand-written", encoding="utf-8")
    result = runner.invoke(cli, ["clean", "--vaults", "--yes"])
    assert "Skipped" in result.output and (stray / "important.md").exists()


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
    assert "All 6 checks passed" in result.output
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


def _built_vault(env) -> Path:
    d = env.work / "vault" / "chan" / "compendiums"
    d.mkdir(parents=True)
    (d / "Alpha.md").write_text("---\ndomain: Alpha\n---\nbody", encoding="utf-8")
    return d


def _record_uploads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    uploads: list[str] = []

    def fake_sync(self, title, source_dir, progress=None):
        uploads.append(title)
        return SyncResult("nb-1", True, 1, 0)

    monkeypatch.setattr(AlchemyEngine, "sync_to_notebooklm", fake_sync)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))
    return uploads


def test_profile_option_and_env_select_the_profile_for_every_notebooklm_call(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _built_vault(isolated_environment)
    profile_dir = isolated_environment.home / ".notebooklm" / "profiles" / "work"
    profile_dir.mkdir(parents=True)
    (profile_dir / "storage_state.json").write_text("{}", encoding="utf-8")
    fake = FakeNotebookLM()
    monkeypatch.setattr(subprocess, "run", fake)

    result = runner.invoke(cli, ["sync", "-n", "chan", "--profile", "work", "--yes"])
    assert result.exit_code == 0, result.output
    assert fake.calls and all(c[1:3] == ["--profile", "work"] for c in fake.calls)
    assert "Upload account: owner@example.test" in result.output and "profile work" in result.output

    fake.calls.clear()
    monkeypatch.setenv("ALCHEMY_NOTEBOOKLM_PROFILE", "work")
    assert runner.invoke(cli, ["sync", "-n", "chan", "--yes"]).exit_code == 0
    assert fake.calls and all(c[1:3] == ["--profile", "work"] for c in fake.calls)


def test_invalid_profile_option_is_a_config_error(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["status", "--profile", "../x"])
    assert result.exit_code == 2 and "profile name" in result.output


def test_unknown_upload_account_is_shown_as_unknown(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(
        NotebookLMClient,
        "upload_account",
        lambda self: AccountIdentity(self.profile, None, "the sign-in did not record an account"),
    )
    _built_vault(isolated_environment)
    uploads = _record_uploads(monkeypatch)
    result = runner.invoke(cli, ["sync", "-n", "chan", "--yes"])
    assert result.exit_code == 0, result.output
    shown = result.output.split("Upload account:")[1].split("\n")[0]
    assert shown.strip().startswith("unknown") and "did not record" in shown
    assert "@" not in shown  # nothing guessed
    assert "login --account" in result.output and uploads == ["chan"]


def test_status_shows_account_and_key_state_but_never_the_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "AIzaSyTEST_ONLY_not_a_real_key_000"
    monkeypatch.setenv("GEMINI_API_KEY", key)
    monkeypatch.setattr(NotebookLMClient, "cli_version", lambda self: "0.8.6")
    result = runner.invoke(cli, ["status", "--profile", "personal"])
    assert result.exit_code == 0, result.output
    assert "owner@example.test" in result.output and "personal" in result.output
    assert "subscription plan" in result.output and key not in result.output and key[:10] not in result.output


def test_first_upload_without_a_terminal_needs_yes(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _built_vault(isolated_environment)
    uploads = _record_uploads(monkeypatch)
    result = runner.invoke(cli, ["sync", "-n", "chan"])
    assert result.exit_code == 1 and "first upload" in result.output and "--yes" in result.output
    assert uploads == []
    assert runner.invoke(cli, ["sync", "-n", "chan", "--yes"]).exit_code == 0
    assert uploads == ["chan"]
    assert not upload_confirmed(AccountIdentity("default", ACCOUNT))  # --yes skips; it does not consent


def test_first_upload_asks_once_per_account(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _built_vault(isolated_environment)
    uploads = _record_uploads(monkeypatch)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)

    declined = runner.invoke(cli, ["sync", "-n", "chan"], input="n\n")
    assert declined.exit_code == 1 and "Nothing was uploaded" in declined.output and uploads == []
    assert "owner@example.test" in declined.output

    accepted = runner.invoke(cli, ["sync", "-n", "chan"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert uploads == ["chan"]
    again = runner.invoke(cli, ["sync", "-n", "chan"])  # no input needed the second time
    assert again.exit_code == 0 and "Upload to this account?" not in again.output

    monkeypatch.setattr(
        NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, "new@example.test")
    )
    changed = runner.invoke(cli, ["sync", "-n", "chan"], input="n\n")
    assert changed.exit_code == 1 and "Upload to this account?" in changed.output
    assert uploads == ["chan", "chan"]


def test_run_asks_before_building_and_shows_key_state(
    runner: CliRunner, fake_channel, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    fake_channel("Build a REST API", "CSS grid tricks")
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))
    uploads = _record_uploads(monkeypatch)
    result = runner.invoke(cli, ["run", "@my", "--offline", "-N", "X"])
    assert result.exit_code == 1 and "first upload" in result.output
    assert "Upload account: owner@example.test" in result.output
    assert "Gemini API key: not set" in result.output
    assert uploads == [] and not (isolated_environment.work / "vault").exists()


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
    key = "AIzaSyTEST_ONLY_setup_key_00000000"
    vault = isolated_environment.work / "my vault"
    # key, profile, sign-in choice, vault
    result = runner.invoke(cli, ["setup"], input=f"{key}\npersonal\nkeep\n{vault}\n")
    assert result.exit_code == 0, result.output
    assert key not in result.output and "Setup complete" in result.output
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# keep me\nALCHEMY_LOG_LEVEL=INFO\n")
    assert f"GEMINI_API_KEY={key}" in text and "ALCHEMY_NOTEBOOKLM_PROFILE=personal" in text
    assert f'ALCHEMY_VAULT_PATH="{vault}"' in text  # as typed

    for name in ("GEMINI_API_KEY", "ALCHEMY_NOTEBOOKLM_PROFILE", "ALCHEMY_VAULT_PATH"):
        os.environ.pop(name, None)  # loaded into this process by the first run
    rerun = runner.invoke(cli, ["setup"], input="\n\nkeep\n\n")  # Enter keeps everything
    assert rerun.exit_code == 0, rerun.output
    assert "Currently: set" in rerun.output and key not in rerun.output
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
    result = runner.invoke(cli, ["setup"], input="short\n\n\ncookies\nedge\n\n\n")
    assert result.exit_code == 0, result.output
    assert "does not look like an API key" in result.output
    assert calls == [
        ["notebooklm", "--profile", "default", "login", "--browser-cookies", "edge", "--account", ACCOUNT]
    ]
    text = (isolated_environment.work / ".env").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY" not in text and "ALCHEMY_NOTEBOOKLM_PROFILE=default" in text


# --- review fixes ------------------------------------------------------------------------------------


def test_an_unknown_account_is_asked_about_every_time(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, None))
    _built_vault(isolated_environment)
    uploads = _record_uploads(monkeypatch)
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    first = runner.invoke(cli, ["sync", "-n", "chan"], input="y\n")
    assert first.exit_code == 0, first.output
    second = runner.invoke(cli, ["sync", "-n", "chan"], input="n\n")
    assert second.exit_code == 1 and "Upload to this account?" in second.output
    assert uploads == ["chan"]


def test_signed_in_email_is_printed_literally(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    odd = "[bold red]x[/bold red]@example.test"
    monkeypatch.setattr(NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, odd))
    monkeypatch.setattr(NotebookLMClient, "browser_accounts", lambda self, b: [odd])
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
    result = runner.invoke(cli, ["login", "--browser-cookies", "edge", "--account", odd])
    assert result.exit_code == 0, result.output
    assert f"Upload account: {odd}" in result.output


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
    result = runner.invoke(cli, ["setup"], input="\npersonal\nkeep\n\n")
    assert result.exit_code == 0, result.output
    assert "ALCHEMY_NOTEBOOKLM_PROFILE is also set in your environment" in result.output
    assert "ALCHEMY_VAULT_PATH is also set" not in result.output


def test_setup_repairs_a_bad_profile_in_env(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    env = isolated_environment.work / ".env"
    env.write_text("ALCHEMY_NOTEBOOKLM_PROFILE=bad/name\n", encoding="utf-8")
    assert runner.invoke(cli, ["status"]).exit_code == 2  # the bad value stops normal commands
    os.environ.pop("ALCHEMY_NOTEBOOKLM_PROFILE", None)
    result = runner.invoke(cli, ["setup"], input="\nfixed\nkeep\n\n")
    assert result.exit_code == 0, result.output
    assert "Setup will ask for a new one" in result.output
    assert "ALCHEMY_NOTEBOOKLM_PROFILE=fixed" in env.read_text(encoding="utf-8")
    assert "bad/name" not in env.read_text(encoding="utf-8")


def test_sync_checks_the_session_before_asking(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, isolated_environment
) -> None:
    _built_vault(isolated_environment)
    uploads = _record_uploads(monkeypatch)
    monkeypatch.setattr(
        NotebookLMClient, "check_session", lambda self: SessionStatus(False, "No NotebookLM session found.")
    )
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    result = runner.invoke(cli, ["sync", "-n", "chan"])
    assert result.exit_code == 1 and "No NotebookLM session" in result.output
    assert "notebook-alchemy login" in result.output and "Upload to this account?" not in result.output
    assert uploads == []


def test_run_stops_if_the_account_changes_during_the_build(
    runner: CliRunner, fake_channel, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_channel("Build a REST API", "CSS grid tricks")
    uploads = _record_uploads(monkeypatch)
    seen = iter([ACCOUNT, "other@example.test"])
    monkeypatch.setattr(
        NotebookLMClient, "upload_account", lambda self: AccountIdentity(self.profile, next(seen))
    )
    result = runner.invoke(cli, ["run", "@my", "--offline", "-N", "My Notebook", "--yes"])
    assert result.exit_code == 1 and "changed during the build" in result.output
    assert "other@example.test" in result.output and uploads == []


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
    result = runner.invoke(cli, ["login", "--browser-cookies", "edge"])
    assert result.exit_code == 0, result.output
    assert calls[0][:3] == ["notebooklm", "--storage", str(storage)] and "login" in calls[0]
    assert "session file" in result.output
