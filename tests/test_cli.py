"""Command line behaviour: output, exit codes, safety."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from notebook_alchemy import __version__, ui
from notebook_alchemy.channel import ChannelScanner, ChannelScanResult, ChannelVideo
from notebook_alchemy.cli import cli
from notebook_alchemy.client import NotebookLMClient, SessionStatus
from notebook_alchemy.engine import AlchemyEngine, SyncResult
from notebook_alchemy.errors import ChannelScanError, SyncError


@pytest.fixture(autouse=True)
def reset_ui() -> None:
    yield
    ui.console = Console(highlight=False)
    ui.err_console = Console(stderr=True, highlight=False)
    ui.UiState.quiet = ui.UiState.debug = False


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _scan_result(*titles: str) -> ChannelScanResult:
    videos = [
        ChannelVideo(f"vid{i:08d}", t, f"https://www.youtube.com/watch?v=vid{i:08d}", 120)
        for i, t in enumerate(titles)
    ]
    return ChannelScanResult("My Channel", "https://www.youtube.com/@my/videos", len(videos), videos, True)


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


@pytest.mark.parametrize("command", ["run", "dry-run", "login", "sync", "append", "clean"])
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
        isolated_environment.work / "vault" / "sample_catalog_mock_data" / "compendiums" / "INDEX.md"
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
    assert (isolated_environment.work / "vault" / "my_channel" / "compendiums" / "INDEX.md").exists()
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
    result = runner.invoke(cli, ["sync", "-n", "chan"])
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
    (vault / "INDEX.md").write_text("index", encoding="utf-8")
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
