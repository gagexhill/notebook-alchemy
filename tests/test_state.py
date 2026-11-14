"""Moving the working files to another computer: export-state and import-state."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from notebook_alchemy.cli import cli
from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.engine import AlchemyEngine
from notebook_alchemy.errors import AlchemyError
from notebook_alchemy.state import FORMAT, MANIFEST, export_state, import_state


def _engine(root: Path) -> AlchemyEngine:
    return AlchemyEngine(AlchemyConfig(cache_dir=root / "cache"))


def _put(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _seed(engine: AlchemyEngine) -> None:
    _put(engine.config.cache_dir / "vid00000001.json", {"video_id": "vid00000001", "text": "words"})
    _put(engine.knowledge_dir() / "vid00000001.json", {"key": "k", "segments": []})
    _put(engine.library_cache_path("Chan"), {"map:Chan": {"title": "T", "topics": []}})
    _put(engine.sync_records_dir() / "nb-test.json", {"titles": ["A"], "ids": {"A": "src-1"}})
    _put(engine.notebooks_path(), {"chan": "nb-test"})
    _put(engine.batch_state_path("Chan"), {"job": "msgbatch_test"})  # still running: not moved
    _put(engine.home / "consent.json", {"account": "salted-hash"})  # belongs to an account: not moved


def _files(engine: AlchemyEngine) -> dict[str, str]:
    return {
        str(p.relative_to(engine.home)).replace("\\", "/"): p.read_text(encoding="utf-8")
        for p in engine.home.rglob("*.json")
    }


def test_the_working_files_move_to_another_computer(tmp_path: Path) -> None:
    here, there = _engine(tmp_path / "a"), _engine(tmp_path / "b")
    _seed(here)
    archive = tmp_path / "state.zip"
    exported = export_state(here, archive)
    assert exported.files == {"captions": 1, "knowledge": 1, "library": 1, "sync": 1, "notebooks": 1}
    imported = import_state(there, archive)
    assert imported.total == 5 and imported.kept == 0
    moved = _files(there)
    assert moved == {k: v for k, v in _files(here).items() if k not in {"batches/Chan.json", "consent.json"}}


def test_import_keeps_what_is_here_unless_told_to_replace(tmp_path: Path) -> None:
    here, there = _engine(tmp_path / "a"), _engine(tmp_path / "b")
    _seed(here)
    export_state(here, tmp_path / "state.zip")
    _put(there.knowledge_dir() / "vid00000001.json", {"key": "newer", "segments": []})
    _put(there.notebooks_path(), {"chan": "nb-here", "other": "nb-2"})
    summary = import_state(there, tmp_path / "state.zip")
    assert summary.kept == 2  # the label file, and the notebook list (this computer's entries win)
    assert json.loads((there.knowledge_dir() / "vid00000001.json").read_text())["key"] == "newer"
    assert json.loads(there.notebooks_path().read_text()) == {"chan": "nb-here", "other": "nb-2"}
    import_state(there, tmp_path / "state.zip", replace=True)
    assert json.loads((there.knowledge_dir() / "vid00000001.json").read_text())["key"] == "k"


def _archive(path: Path, members: dict[str, str], version: int = 1) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(MANIFEST, json.dumps({"format": FORMAT, "version": version}))
        for name, text in members.items():
            z.writestr(name, text)
    return path


@pytest.mark.parametrize(
    "name",
    ["../evil.json", "captions/../../evil.json", "other/x.json", "captions/sub/x.json", "/abs.json", "x.txt"],
)
def test_an_archive_with_a_file_this_tool_does_not_write_is_refused_whole(tmp_path: Path, name: str) -> None:
    there = _engine(tmp_path / "b")
    bad = _archive(tmp_path / "bad.zip", {"captions/ok.json": '{"video_id": "v"}', name: "{}"})
    with pytest.raises(AlchemyError, match="does not write"):
        import_state(there, bad)
    assert not there.home.exists() or not any(there.home.rglob("*"))  # nothing was written
    assert not (tmp_path / "evil.json").exists()


def test_a_file_that_is_not_a_json_object_is_refused(tmp_path: Path) -> None:
    there = _engine(tmp_path / "b")
    with pytest.raises(AlchemyError, match="not a JSON object"):
        import_state(there, _archive(tmp_path / "bad.zip", {"captions/a.json": "[1, 2]"}))


def test_a_newer_format_or_a_foreign_file_is_refused(tmp_path: Path) -> None:
    there = _engine(tmp_path / "b")
    with pytest.raises(AlchemyError, match="newer version"):
        import_state(there, _archive(tmp_path / "new.zip", {}, version=99))
    (tmp_path / "plain.zip").write_bytes(b"not a zip")
    with pytest.raises(AlchemyError, match="not a Notebook Alchemy state file"):
        import_state(there, tmp_path / "plain.zip")
    with zipfile.ZipFile(tmp_path / "other.zip", "w") as z:
        z.writestr("readme.txt", "hello")
    with pytest.raises(AlchemyError, match="not a Notebook Alchemy state file"):
        import_state(there, tmp_path / "other.zip")


def test_export_never_overwrites_a_file(tmp_path: Path) -> None:
    (tmp_path / "state.zip").write_text("mine", encoding="utf-8")
    with pytest.raises(AlchemyError, match="already exists"):
        export_state(_engine(tmp_path / "a"), tmp_path / "state.zip")
    assert (tmp_path / "state.zip").read_text(encoding="utf-8") == "mine"


def test_the_commands_report_what_moved(isolated_environment) -> None:
    runner = CliRunner()
    _seed(AlchemyEngine(AlchemyConfig.from_env()))
    out = runner.invoke(cli, ["export-state", "state.zip"])
    assert out.exit_code == 0, out.output
    assert "Saved [state.zip]." in out.output and "notebook-alchemy import-state state.zip" in out.output
    back = runner.invoke(cli, ["import-state", "state.zip"])
    assert back.exit_code == 0, back.output
    assert "Imported 0 files" in back.output and "Kept 5 files" in back.output


def test_an_oversized_archive_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import notebook_alchemy.state as state

    monkeypatch.setattr(state, "MAX_TOTAL_BYTES", 30)
    members = {f"captions/v{i}.json": '{"video_id": "v"}' for i in range(3)}
    with pytest.raises(AlchemyError, match="larger than"):
        import_state(_engine(tmp_path / "b"), _archive(tmp_path / "big.zip", members))


# --- review findings ---------------------------------------------------------------------------------


def test_a_channel_named_in_any_script_moves_too(tmp_path: Path) -> None:
    here, there = _engine(tmp_path / "a"), _engine(tmp_path / "b")
    _put(here.library_cache_path("日本語チャンネル"), {"map": 1})
    _put(here.library_cache_path("Café Talks"), {"map": 2})
    summary = export_state(here, tmp_path / "state.zip")
    assert summary.files == {"library": 2} and not summary.skipped
    import_state(there, tmp_path / "state.zip")
    assert json.loads(there.library_cache_path("日本語チャンネル").read_text(encoding="utf-8")) == {"map": 1}


def test_export_names_what_it_left_out(tmp_path: Path) -> None:
    here = _engine(tmp_path / "a")
    _put(here.knowledge_dir() / "odd name!.json", {"x": 1})
    (here.knowledge_dir() / "broken.json").write_text("{", encoding="utf-8")
    summary = export_state(here, tmp_path / "state.zip")
    assert sorted(summary.skipped) == [
        "knowledge/broken.json (not readable JSON)",
        "knowledge/odd name!.json (a name this tool does not write)",
    ]


@pytest.mark.parametrize("name", ["captions/NUL.json", "sync/com1.json"])
def test_a_device_name_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(AlchemyError, match="does not write"):
        import_state(_engine(tmp_path / "b"), _archive(tmp_path / "bad.zip", {name: "{}"}))


def test_deeply_nested_json_is_refused_not_a_crash(tmp_path: Path) -> None:
    deep = "[" * 100_000 + "]" * 100_000
    with pytest.raises(AlchemyError, match="not a JSON object"):
        import_state(_engine(tmp_path / "b"), _archive(tmp_path / "deep.zip", {"captions/a.json": deep}))


def test_a_member_that_inflates_past_the_cap_is_refused(tmp_path: Path, monkeypatch) -> None:
    import notebook_alchemy.state as state

    monkeypatch.setattr(state, "MAX_FILE_BYTES", 100)
    big = json.dumps({"video_id": "v", "text": "word " * 100})
    with pytest.raises(AlchemyError, match="larger than"):
        import_state(_engine(tmp_path / "b"), _archive(tmp_path / "big.zip", {"captions/a.json": big}))


def test_an_import_leaves_no_temporary_files(tmp_path: Path) -> None:
    here, there = _engine(tmp_path / "a"), _engine(tmp_path / "b")
    _seed(here)
    export_state(here, tmp_path / "state.zip")
    import_state(there, tmp_path / "state.zip")
    assert not list(there.home.rglob("*.importing"))


def test_a_damaged_or_lying_member_is_a_clear_error(tmp_path: Path) -> None:
    import notebook_alchemy.state as state

    archive_path = _archive(tmp_path / "lying.zip", {"captions/a.json": json.dumps({"t": "word " * 100})})
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo("captions/a.json")
        info.file_size = 10  # what a crafted header could claim
        with pytest.raises(AlchemyError, match="is damaged"):
            state._read_member(archive, info)
