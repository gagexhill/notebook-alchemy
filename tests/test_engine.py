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

from .conftest import FakeNotebookLM, FakeProcess

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


def _fake_engine(tmp_path: Path, fake: FakeNotebookLM) -> AlchemyEngine:
    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    engine.client = NotebookLMClient(engine.config, runner=fake)
    return engine


def test_sync_creates_the_notebook_and_uploads_every_file(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    result = _fake_engine(tmp_path, fake).sync_to_notebooklm("Chan", _real_dir(tmp_path))
    assert (result.created, result.uploaded, result.replaced) == (True, 2, 0)
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics"]


def test_resync_replaces_old_copies_including_filename_titled_ones(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    fake.seed("Chan", ["Alpha Topics", "Alpha Topics", "Beta_Topics.md", "My own notes"])
    result = _fake_engine(tmp_path, fake).sync_to_notebooklm("Chan", _real_dir(tmp_path))
    assert result.replaced == 3  # both exact copies and the filename-titled one
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics", "My own notes"]  # user's own source kept


def test_titles_the_service_resets_to_file_names_are_corrected(tmp_path: Path) -> None:
    fake = FakeNotebookLM(reset_titles=True)  # the behaviour seen against the real NotebookLM
    engine = _fake_engine(tmp_path, fake)
    directory = _real_dir(tmp_path)
    engine.sync_to_notebooklm("Chan", directory)
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics"]
    engine.sync_to_notebooklm("Chan", directory)  # a second run must not pile up duplicates
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics"]


def test_sync_adds_before_it_deletes_and_waits_before_renaming(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    fake.seed("Chan", ["Alpha Topics"])
    _fake_engine(tmp_path, fake).sync_to_notebooklm("Chan", _real_dir(tmp_path))
    verbs = [" ".join(c) for c in fake.calls]
    first_add = next(i for i, c in enumerate(verbs) if "source add" in c)
    first_wait = next(i for i, c in enumerate(verbs) if "source wait" in c)
    first_delete = next(i for i, c in enumerate(verbs) if "source delete" in c)
    assert first_add < first_wait < first_delete  # the old copy goes only after the new one is ready


def test_sync_reports_each_failed_file_and_still_uploads_the_rest(tmp_path: Path) -> None:
    fake = FakeNotebookLM(fail={"add": "Beta_Topics.md"})
    with pytest.raises(SyncError) as err:
        _fake_engine(tmp_path, fake).sync_to_notebooklm("Chan", _real_dir(tmp_path))
    assert err.value.failed == ["Beta_Topics.md: `notebooklm source add` failed: add failed on purpose"]
    assert fake.titles("Chan") == ["Alpha Topics"]


def test_an_old_copy_that_cannot_be_deleted_is_reported(tmp_path: Path) -> None:
    fake = FakeNotebookLM(fail={"delete": "old-"})
    fake.seed("Chan", ["Alpha Topics"])
    with pytest.raises(SyncError) as err:
        _fake_engine(tmp_path, fake).sync_to_notebooklm("Chan", _real_dir(tmp_path))
    assert any("old copy not removed" in f for f in err.value.failed)
    assert any("appears 2 times" in f for f in err.value.failed)  # the read-back catches the duplicate


def test_a_source_that_never_finishes_processing_is_reported(tmp_path: Path) -> None:
    fake = FakeNotebookLM(fail={"wait": "src-"})
    with pytest.raises(SyncError) as err:
        _fake_engine(tmp_path, fake).sync_to_notebooklm("Chan", _real_dir(tmp_path))
    assert all("processing did not finish" in f for f in err.value.failed) and len(err.value.failed) == 2


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


def _write_set(tmp_path: Path, names: tuple[str, ...]) -> Path:
    directory = tmp_path / "vault" / "chan" / "compendiums"
    if directory.exists():
        for old in directory.glob("*.md"):
            old.unlink()
    directory.mkdir(parents=True, exist_ok=True)
    for n in names:
        (directory / f"{n}.md").write_text(
            f'---\ndomain: "{n}"\nvolume: 1\nsource_count: 1\n---\n# {n}\n', encoding="utf-8"
        )
    return directory


def test_a_later_run_with_different_domains_prunes_the_earlier_runs_sources(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    fake.seed("Chan", ["My own notes"])
    engine.sync_to_notebooklm("Chan", _write_set(tmp_path, ("Alpha_Topics", "Beta_Topics")))
    second = engine.sync_to_notebooklm("Chan", _write_set(tmp_path, ("Gamma_Topics", "Beta_Topics")))
    assert second.pruned == 1  # Alpha Topics is gone from this build
    assert fake.titles("Chan") == ["Beta Topics", "Gamma Topics", "My own notes"]


def test_nothing_is_pruned_without_a_record_of_an_earlier_sync(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    fake.seed("Chan", ["Old Domain", "My own notes"])
    result = _fake_engine(tmp_path, fake).sync_to_notebooklm("Chan", _real_dir(tmp_path))
    assert result.pruned == 0 and "Old Domain" in fake.titles("Chan")


def test_a_failed_sync_does_not_overwrite_the_record(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    engine.sync_to_notebooklm("Chan", _write_set(tmp_path, ("Alpha_Topics",)))
    broken = FakeNotebookLM(fail={"add": "Gamma"})
    broken.notebooks, broken.sources = fake.notebooks, fake.sources
    engine.client = NotebookLMClient(engine.config, runner=broken)
    with pytest.raises(SyncError):
        engine.sync_to_notebooklm("Chan", _write_set(tmp_path, ("Gamma_Topics",)))
    engine.client = NotebookLMClient(engine.config, runner=fake)
    engine.sync_to_notebooklm("Chan", _write_set(tmp_path, ("Gamma_Topics",)))
    assert fake.titles("Chan") == ["Gamma Topics"]  # Alpha was still recorded, so it is pruned now
