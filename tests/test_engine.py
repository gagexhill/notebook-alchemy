"""Engine: vault layout, atomic writes, sync and append."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notebook_alchemy.analyzer import VideoSourceSummary
from notebook_alchemy.client import NotebookLMClient
from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.engine import AlchemyEngine
from notebook_alchemy.errors import AlchemyError, ConfigError, SessionError, SyncError, VaultError
from notebook_alchemy.synthesizer import Synthesizer

from .conftest import FakeProcess

MOCK_DIR = "sample_catalog_mock_data"


def _config(tmp_path: Path) -> AlchemyConfig:
    return AlchemyConfig(
        vault_path=tmp_path / "vault",
        state_dir=tmp_path / "state",
        cache_dir=tmp_path / "cache",
        gemini_api_key="",
    )


@pytest.fixture
def engine(tmp_path: Path) -> AlchemyEngine:
    return AlchemyEngine(_config(tmp_path))


def _session(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "storage_state.json").write_text("{}", encoding="utf-8")


def _real_dir(tmp_path: Path, names: tuple[str, ...] = ("Alpha_Topics", "Beta_Topics")) -> Path:
    d = tmp_path / "vault" / "chan" / "compendiums"
    d.mkdir(parents=True)
    for n in names:
        (d / f"{n}.md").write_text(
            f'---\ndomain: "{n}"\nvolume: 1\nsource_count: 1\n---\n# {n}\n', encoding="utf-8"
        )
    return d


# --- distillation ---------------------------------------------------------------------------------


def test_output_is_namespaced_per_source(engine: AlchemyEngine, tmp_path: Path) -> None:
    other = tmp_path / "vault" / "another_channel" / "compendiums"
    other.mkdir(parents=True)
    (other / "keep.md").write_text("other channel", encoding="utf-8")

    report = engine.run_distillation(use_mock=True, force_offline=True)
    final = tmp_path / "vault" / MOCK_DIR / "compendiums"
    assert report.vault_directory == final
    assert (final / "INDEX.md").exists() and (final / "MASTER_SOURCES_CATALOG.md").exists()
    assert not (tmp_path / "vault" / "compendiums").exists()
    assert (other / "keep.md").read_text(encoding="utf-8") == "other channel"
    assert report.is_mock


def test_every_video_lands_in_exactly_one_volume(engine: AlchemyEngine) -> None:
    report = engine.run_distillation(use_mock=True, force_offline=True, max_videos_per_volume=20)
    ids = [i for c in report.compendiums for i in c.source_ids]
    assert len(ids) == len(set(ids)) == 288
    assert all(len(c.titles) <= 20 for c in report.compendiums)
    assert len({c.relative_path for c in report.compendiums}) == len(report.compendiums)


def test_catalog_records_each_videos_domain(engine: AlchemyEngine, tmp_path: Path) -> None:
    engine.run_distillation(use_mock=True, force_offline=True)
    catalog = (tmp_path / "vault" / MOCK_DIR / "compendiums" / "MASTER_SOURCES_CATALOG.md").read_text(
        encoding="utf-8"
    )
    assert "Distilled Source" not in catalog
    rows = [line for line in catalog.splitlines() if line.startswith("| ") and "`mock_" in line]
    assert len(rows) == 288 and all(r.split("|")[4].strip() for r in rows)


def test_failed_run_leaves_the_previous_vault_intact(
    engine: AlchemyEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.run_distillation(use_mock=True, force_offline=True)
    final = tmp_path / "vault" / MOCK_DIR / "compendiums"
    marker = final / "my_notes.md"
    marker.write_text("user file", encoding="utf-8")

    def boom(self: Synthesizer, *a: object, **k: object) -> str:
        raise RuntimeError("disk full")

    monkeypatch.setattr(Synthesizer, "generate_index", boom)
    with pytest.raises(RuntimeError):
        engine.run_distillation(use_mock=True, force_offline=True)
    assert marker.read_text(encoding="utf-8") == "user file"  # old build untouched
    assert not final.with_name("compendiums.tmp").exists()  # no staging debris


def test_preview_only_writes_nothing(engine: AlchemyEngine, tmp_path: Path) -> None:
    report = engine.run_distillation(use_mock=True, force_offline=True, write_to_vault=False)
    assert report.vault_directory is None and not (tmp_path / "vault").exists()


def test_max_per_vol_is_validated(engine: AlchemyEngine) -> None:
    with pytest.raises(ConfigError):
        engine.run_distillation(use_mock=True, max_videos_per_volume=0)


def test_duplicate_titles_keep_distinct_ids(engine: AlchemyEngine) -> None:
    videos = [VideoSourceSummary(f"id{i}", "Same Title", f"https://u/{i}", "yt", "ready") for i in range(4)]
    report = engine.analyzer._build_report(
        nb_id="nb_dup",
        nb_title="Dup",
        videos=videos,
        others=[],
        total_sources=4,
        origin="youtube",
        progress=None,
    )
    result = engine.run_distillation(dry_run=report, force_offline=True)
    assert sorted(i for c in result.compendiums for i in c.source_ids) == [
        "id0",
        "id1",
        "id2",
        "id3",
    ]


def test_source_without_videos_is_an_error(engine: AlchemyEngine) -> None:
    report = engine.analyzer._build_report(
        nb_id="nb",
        nb_title="Empty",
        videos=[],
        others=[],
        total_sources=0,
        origin="youtube",
        progress=None,
    )
    with pytest.raises(AlchemyError, match="no video sources"):
        engine.run_distillation(dry_run=report)


def test_status_reports_facts_without_network(engine: AlchemyEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NotebookLMClient, "cli_version", lambda self: "notebooklm 9.9")
    facts = engine.status()
    assert facts["notebooklm_cli"] == "notebooklm 9.9"
    assert facts["gemini_api_key"] == "unset"
    assert facts["session_file"].startswith("none")
    assert "key" not in json.dumps(facts).lower().replace("gemini_api_key", "")  # no secret values


# --- sync -----------------------------------------------------------------------------------------

LIST_NBS = json.dumps([{"id": "nb-1", "title": "Chan"}])
LIST_SRC = json.dumps([{"id": "s1", "title": "Alpha Topics"}])


def _sync_engine(tmp_path: Path, process: FakeProcess) -> AlchemyEngine:
    engine = AlchemyEngine(_config(tmp_path))
    engine.client = NotebookLMClient(engine.config, runner=process)
    return engine


def test_sync_replaces_same_titled_sources_after_adding(tmp_path: Path) -> None:
    _session(tmp_path)
    process = FakeProcess(
        ("source delete", 0, "{}", ""),
        ("source list", 0, LIST_SRC, ""),
        ("source add", 0, json.dumps({"id": "new"}), ""),
        ("list", 0, LIST_NBS, ""),
    )
    result = _sync_engine(tmp_path, process).sync_to_notebooklm("Chan", _real_dir(tmp_path))
    assert (result.uploaded, result.replaced, result.created) == (2, 1, False)
    commands = [" ".join(c) for c in process.calls]
    add_alpha = next(i for i, c in enumerate(commands) if "Alpha_Topics.md" in c)
    delete = next(i for i, c in enumerate(commands) if "source delete s1" in c)
    assert add_alpha < delete  # the old copy is removed only after the new one is in


def test_sync_reports_each_failed_file_and_still_uploads_the_rest(tmp_path: Path) -> None:
    _session(tmp_path)

    def runner(cmd: list[str], **kw: object):
        import subprocess

        joined = " ".join(cmd)
        if "source add" in joined and "Beta_Topics.md" in joined:
            return subprocess.CompletedProcess(cmd, 2, "", "No such option")
        if "source add" in joined:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": "x"}), "")
        if "source list" in joined:
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        return subprocess.CompletedProcess(cmd, 0, LIST_NBS, "")

    engine = AlchemyEngine(_config(tmp_path))
    engine.client = NotebookLMClient(engine.config, runner=runner)
    with pytest.raises(SyncError) as err:
        engine.sync_to_notebooklm("Chan", _real_dir(tmp_path))
    assert err.value.failed == ["Beta_Topics.md: `notebooklm source add` failed: No such option"]
    assert "1 of 2" in err.value.message


def test_sync_needs_a_working_session(tmp_path: Path) -> None:
    engine = _sync_engine(tmp_path, FakeProcess())
    with pytest.raises(SessionError):
        engine.sync_to_notebooklm("Chan", _real_dir(tmp_path))


def test_sync_refuses_mock_output(engine: AlchemyEngine, tmp_path: Path) -> None:
    _session(tmp_path)
    engine.run_distillation(use_mock=True, force_offline=True)
    process = FakeProcess(("list", 0, LIST_NBS, ""))
    engine.client = NotebookLMClient(engine.config, runner=process)
    with pytest.raises(AlchemyError, match="mock sample data"):
        engine.sync_to_notebooklm("Chan", tmp_path / "vault" / MOCK_DIR / "compendiums")
    assert not any("source add" in " ".join(c) for c in process.calls)


def test_sync_with_no_files_is_an_error(tmp_path: Path) -> None:
    _session(tmp_path)
    engine = _sync_engine(tmp_path, FakeProcess(("list", 0, "[]", "")))
    with pytest.raises(VaultError):
        engine.sync_to_notebooklm("Chan", tmp_path / "missing")


# --- append ---------------------------------------------------------------------------------------


def _built(engine: AlchemyEngine, tmp_path: Path) -> Path:
    report = engine.analyzer._build_report(
        nb_id="nb",
        nb_title="Chan",
        videos=[VideoSourceSummary("a1", "Build a React app", "https://youtu.be/a1", "yt", "ready")],
        others=[],
        total_sources=1,
        origin="youtube",
        progress=None,
    )
    return engine.run_distillation(dry_run=report, force_offline=True).vault_directory  # type: ignore[return-value]


def test_append_adds_once_and_skips_duplicates(engine: AlchemyEngine, tmp_path: Path) -> None:
    directory = _built(engine, tmp_path)
    domain = next(
        p for p in directory.glob("*.md") if p.name not in ("INDEX.md", "MASTER_SOURCES_CATALOG.md")
    ).read_text(encoding="utf-8")
    domain_name = next(line for line in domain.splitlines() if line.startswith("domain:")).split('"')[1]
    video = {"id": "n1", "title": "New video", "url": "https://youtu.be/n1"}

    first = engine.append_incremental_sources([video], directory, domain=domain_name)
    again = engine.append_incremental_sources([video], directory, domain=domain_name)
    assert (first.added, first.skipped) == (1, 0)
    assert (again.added, again.skipped) == (0, 1)
    catalog = (directory / "MASTER_SOURCES_CATALOG.md").read_text(encoding="utf-8")
    assert catalog.count("`n1`") == 1 and "total_sources: 2" in catalog
    body = (directory / first.updated_files[0]).read_text(encoding="utf-8")
    assert "source_count: 2" in body and "[Added] [New video]" in body


def test_append_creates_a_new_domain_file(engine: AlchemyEngine, tmp_path: Path) -> None:
    directory = _built(engine, tmp_path)
    result = engine.append_incremental_sources(
        [{"id": "n2", "title": "Cooking pasta", "url": "https://youtu.be/n2"}],
        directory,
        domain="Cooking",
    )
    assert result.updated_files == ["Cooking.md"]
    assert 'domain: "Cooking"' in (directory / "Cooking.md").read_text(encoding="utf-8")


def test_clustering_and_synthesis_share_one_gemini_client(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.gemini_api_key = "K" * 24
    engine = AlchemyEngine(config)
    assert engine.gemini is not None
    assert engine.analyzer._gemini_client() is engine.gemini
    assert engine.synthesizer._client() is engine.gemini  # a failover seen once is seen by both


def test_no_gemini_client_without_a_key(engine: AlchemyEngine) -> None:
    assert engine.gemini is None
