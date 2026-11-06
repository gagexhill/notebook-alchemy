"""NotebookLM client: session detection and upload behaviour."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from notebook_alchemy.client import NotebookLMClient, SourceDocument
from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.errors import NotebookLMError, SessionError

from .conftest import FakeProcess

NOTEBOOKS = json.dumps([{"id": "nb-1", "title": "Alpha"}, {"id": "nb-2", "title": "Alpha Beta"}])


def _client(tmp_path: Path, process: FakeProcess | None = None, **cfg) -> NotebookLMClient:
    config = AlchemyConfig(vault_path=tmp_path / "v", state_dir=tmp_path / "state", **cfg)
    return NotebookLMClient(config, runner=process)


def test_session_found_in_notebooklm_profile_dir(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert not client.is_authenticated()
    state = client.profile_state_path()
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    assert client.is_authenticated()
    assert client.session_file() == state


def test_session_found_in_state_dir_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "storage_state.json").write_text("{}", encoding="utf-8")
    assert client.is_authenticated()
    other = _client(tmp_path / "elsewhere")
    assert not other.is_authenticated()
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{}")
    assert other.is_authenticated()


def test_check_session_states(tmp_path: Path) -> None:
    missing = _client(tmp_path).check_session()
    assert not missing.ok and "No NotebookLM session" in missing.detail

    state = tmp_path / "state"
    state.mkdir()
    (state / "storage_state.json").write_text("{}", encoding="utf-8")
    good = _client(tmp_path, FakeProcess(("list", 0, NOTEBOOKS, "")))
    status = good.check_session()
    assert status.ok and status.notebooks == 2

    expired = _client(
        tmp_path,
        FakeProcess(("list", 1, json.dumps({"message": "Authentication expired or invalid."}), "")),
    )
    result = expired.check_session()
    assert not result.ok and "expired" in result.detail.lower()


def test_upload_sends_a_file_not_content_and_not_text_flag(tmp_path: Path) -> None:
    uploaded: dict[str, str] = {}

    def runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        path = Path(cmd[cmd.index("add") + 1])
        uploaded["content"] = path.read_text(encoding="utf-8")
        uploaded["cmd"] = " ".join(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": "src-9"}), "")

    client = _client(tmp_path, runner)  # type: ignore[arg-type]
    big = "x" * 60_000  # larger than the Windows command-line limit
    src_id = client.add_source_document("nb-1", SourceDocument("a", "Big Doc", big))
    assert src_id == "src-9"
    assert uploaded["content"] == big
    assert "--text" not in uploaded["cmd"]
    assert big not in uploaded["cmd"]
    assert "--type file" in uploaded["cmd"] and "--title Big Doc" in uploaded["cmd"]


def test_upload_failure_raises_with_cause(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeProcess(("source add", 2, "", "No such option")))
    with pytest.raises(NotebookLMError) as err:
        client.add_source_document("nb-1", SourceDocument("a", "Doc", "body"))
    assert "No such option" in err.value.message


def test_timeout_and_missing_binary_are_typed_errors(tmp_path: Path) -> None:
    def slow(cmd: list[str], **kwargs: object):
        raise subprocess.TimeoutExpired(cmd, 1)

    def absent(cmd: list[str], **kwargs: object):
        raise FileNotFoundError("notebooklm")

    with pytest.raises(NotebookLMError, match="did not finish"):
        _client(tmp_path, slow).list_notebooks()  # type: ignore[arg-type]
    with pytest.raises(NotebookLMError) as err:
        _client(tmp_path, absent).list_notebooks()  # type: ignore[arg-type]
    assert "pip install notebooklm-py" in (err.value.hint or "")


def test_unparseable_output_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(NotebookLMError, match="not JSON"):
        _client(tmp_path, FakeProcess(("list", 0, "<html>", ""))).list_notebooks()


def test_auth_failure_is_a_session_error(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeProcess(("list", 1, "", "Authentication expired")))
    with pytest.raises(SessionError):
        client.list_notebooks()


def test_find_notebook_exact_then_unique_partial_and_ambiguity(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeProcess(("list", 0, NOTEBOOKS, "")))
    assert client.find_notebook("alpha")["id"] == "nb-1"  # exact title wins over the longer title
    assert client.find_notebook("nb-2")["title"] == "Alpha Beta"
    assert client.find_notebook("beta")["id"] == "nb-2"
    assert client.find_notebook("zzz") is None
    ambiguous = _client(
        tmp_path,
        FakeProcess(
            (
                "list",
                0,
                json.dumps([{"id": "1", "title": "Data A"}, {"id": "2", "title": "Data B"}]),
                "",
            )
        ),
    )
    with pytest.raises(NotebookLMError, match="matches 2 notebooks"):
        ambiguous.find_notebook("data")


def test_create_notebook_reuses_exact_title_only(tmp_path: Path) -> None:
    process = FakeProcess(("create", 0, json.dumps({"id": "new-1"}), ""), ("list", 0, NOTEBOOKS, ""))
    client = _client(tmp_path, process)
    assert client.create_notebook("Alpha") == ("nb-1", False)  # exact title reused
    assert client.create_notebook("Alph") == ("new-1", True)  # a prefix is not a match
    assert any("create" in " ".join(c) for c in process.calls)


def test_create_without_id_is_an_error(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeProcess(("create", 0, "{}", ""), ("list", 0, "[]", "")))
    with pytest.raises(NotebookLMError, match="no notebook id"):
        client.create_notebook("New")


def test_a_missing_session_file_setting_is_not_passed_to_the_cli(tmp_path: Path) -> None:
    process = FakeProcess(("list", 0, NOTEBOOKS, ""))
    missing = tmp_path / "old-env" / "session.json"  # for example copied from an old .env
    _client(tmp_path, process, session_file=missing).list_notebooks()
    assert "--storage" not in process.calls[0]


def test_an_existing_session_file_setting_is_passed_to_the_cli(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    session.write_text("{}", encoding="utf-8")
    process = FakeProcess(("list", 0, NOTEBOOKS, ""))
    _client(tmp_path, process, session_file=session).list_notebooks()
    assert process.calls[0][process.calls[0].index("--storage") + 1] == str(session)


def test_auth_not_found_is_a_session_error(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeProcess(("list", 1, json.dumps({"message": "Auth not found."}), "")))
    with pytest.raises(SessionError):
        client.list_notebooks()


def test_check_session_reports_any_cli_failure_instead_of_raising(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "storage_state.json").write_text("{}", encoding="utf-8")
    client = _client(tmp_path, FakeProcess(("list", 1, "", "something unexpected broke")))
    status = client.check_session()
    assert not status.ok and "something unexpected broke" in status.detail
