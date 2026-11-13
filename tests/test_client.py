"""NotebookLM client: session detection and upload behaviour."""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
from pathlib import Path

import pytest

from notebook_alchemy.client import (
    AccountIdentity,
    NotebookLMClient,
    PlanLimits,
    SourceDocument,
    consent_file,
    normalize_title,
    record_upload_consent,
    upload_confirmed,
)
from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.errors import ConfigError, NotebookLMError, SessionError

from .conftest import FakeProcess

NOTEBOOKS = json.dumps([{"id": "nb-1", "title": "Alpha"}, {"id": "nb-2", "title": "Alpha Beta"}])


def _client(tmp_path: Path, process: FakeProcess | None = None, **cfg) -> NotebookLMClient:
    config = AlchemyConfig(cache_dir=tmp_path / "c", **cfg)
    return NotebookLMClient(config, runner=process)


def test_session_found_in_notebooklm_profile_dir(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert not client.is_authenticated()
    state = client.profile_state_path()
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    assert client.is_authenticated()
    assert client.session_file() == state


def _profile_session(client: NotebookLMClient) -> Path:
    state = client.profile_state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}", encoding="utf-8")
    return state


def test_only_a_session_notebooklm_will_use_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "storage_state.json").write_text("{}", encoding="utf-8")
    assert not client.is_authenticated()  # never passed to notebooklm, so it is not a session
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{}")
    assert client.is_authenticated()


def test_check_session_states(tmp_path: Path) -> None:
    missing = _client(tmp_path).check_session()
    assert not missing.ok and "No NotebookLM session" in missing.detail

    good = _client(tmp_path, FakeProcess(("list", 0, NOTEBOOKS, "")))
    _profile_session(good)
    status = good.check_session()
    assert status.ok and status.notebooks == 2

    expired = _client(
        tmp_path,
        FakeProcess(("list", 1, json.dumps({"message": "Authentication expired or invalid."}), "")),
    )
    result = expired.check_session()
    assert not result.ok and "expired" in result.detail.lower()
    assert missing.signed_out  # no sign-in at all
    _profile_session(expired)
    assert expired.check_session().signed_out  # an ended sign-in


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
    assert "--request-timeout 180" in uploaded["cmd"]  # notebooklm's own 30 s default is too short


def test_an_upload_without_a_source_id_is_an_error(tmp_path: Path) -> None:
    client = _client(tmp_path, FakeProcess(("source add", 0, json.dumps({"status": "ok"}), "")))
    with pytest.raises(NotebookLMError, match="no source id"):
        client.add_source_document("nb-1", SourceDocument("a", "Doc", "body"))


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
    client = _client(tmp_path, FakeProcess(("list", 1, "", "something unexpected broke")))
    _profile_session(client)
    status = client.check_session()
    assert not status.ok and "something unexpected broke" in status.detail
    assert not status.signed_out  # a fault, not a sign-out: signing in again would not help


def test_an_old_unprefixed_title_counts_as_a_copy_of_the_new_one() -> None:
    # A re-sync must replace the INDEX source an earlier version uploaded, not add a second index.
    assert normalize_title("_INDEX") == normalize_title("INDEX") == normalize_title("_INDEX.md")
    assert normalize_title("_MASTER SOURCES CATALOG") == normalize_title("MASTER_SOURCES_CATALOG.md")


# --- profile and upload account ------------------------------------------------------------------


def _auth_check(email: str | None, *, code: int = 0, stored: bool = True) -> str:
    """The shape notebooklm-py 0.8.6 prints: `account` is always present, null when unknown."""
    return json.dumps(
        {
            "status": "ok" if code == 0 else "error",
            "account": {"email": email, "authuser": 0} if email else None,
            "checks": {"storage_exists": stored, "json_valid": stored},
        }
    )


def test_profile_comes_from_config_and_an_explicit_profile_wins(tmp_path: Path) -> None:
    process = FakeProcess(("list", 0, "[]", ""))
    _client(tmp_path, process, notebooklm_profile="work").list_notebooks()
    assert process.calls[-1][:3] == ["notebooklm", "--profile", "work"]
    config = AlchemyConfig(notebooklm_profile="work")
    NotebookLMClient(config, "other", runner=process).list_notebooks()
    assert process.calls[-1][:3] == ["notebooklm", "--profile", "other"]


def test_upload_account_reads_the_profile_record(tmp_path: Path) -> None:
    process = FakeProcess(("auth check", 0, _auth_check("me@example.test"), ""))
    identity = _client(tmp_path, process, notebooklm_profile="work").upload_account()
    assert identity.email == "me@example.test" and identity.profile == "work"
    assert process.calls[0] == ["notebooklm", "--profile", "work", "auth", "check", "--json"]


def test_upload_account_parses_a_failing_check_that_still_reports(tmp_path: Path) -> None:
    process = FakeProcess(("auth check", 1, _auth_check("me@example.test", code=1), ""))
    assert _client(tmp_path, process).upload_account().email == "me@example.test"


@pytest.mark.parametrize(
    ("process", "reason"),
    [
        (FakeProcess(("auth check", 0, _auth_check(None), "")), "did not record"),
        (FakeProcess(("auth check", 1, _auth_check(None, stored=False), "")), "no session"),
        (FakeProcess(("auth check", 0, json.dumps({"account": {"email": "not-an-email"}}), "")), "record"),
        (FakeProcess(("auth check", 2, "Traceback: boom", "")), "no report"),
    ],
)
def test_upload_account_is_unknown_instead_of_guessed(
    tmp_path: Path, process: FakeProcess, reason: str
) -> None:
    identity = _client(tmp_path, process).upload_account()
    assert identity.email is None and identity.display == "unknown" and reason in identity.detail


def test_upload_account_unknown_when_the_cli_is_missing_or_env_auth_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def absent(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    assert "not found" in _client(tmp_path, absent).upload_account().detail
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "{}")
    process = FakeProcess()
    identity = _client(tmp_path, process).upload_account()
    assert identity.email is None and "NOTEBOOKLM_AUTH_JSON" in identity.detail and not process.calls


def test_upload_consent_is_per_profile_and_per_account() -> None:
    first = AccountIdentity("work", "a@example.test")
    assert not upload_confirmed(first)
    record_upload_consent(first)
    assert upload_confirmed(first)
    assert not upload_confirmed(AccountIdentity("work", "b@example.test"))  # account changed
    assert not upload_confirmed(AccountIdentity("work", None))
    assert not upload_confirmed(AccountIdentity("personal", "a@example.test"))


def test_the_consent_record_never_holds_the_email() -> None:
    record_upload_consent(AccountIdentity("work", "Private.Person@example.test"))
    record_upload_consent(AccountIdentity("home", "other@example.test"))
    text = consent_file().read_text(encoding="utf-8")
    assert "example.test" not in text.lower() and "private" not in text.lower()
    assert upload_confirmed(AccountIdentity("work", "private.person@EXAMPLE.test"))  # case-insensitive


def test_a_plain_email_record_from_an_earlier_version_asks_again() -> None:
    consent_file().parent.mkdir(parents=True, exist_ok=True)
    consent_file().write_text(json.dumps({"work": {"email": "a@example.test"}}), encoding="utf-8")
    assert not upload_confirmed(AccountIdentity("work", "a@example.test"))
    record_upload_consent(AccountIdentity("work", "a@example.test"))  # rewrites it in the new form
    assert "a@example.test" not in consent_file().read_text(encoding="utf-8")


def test_unreadable_consent_record_asks_again() -> None:
    record_upload_consent(AccountIdentity("work", "a@example.test"))
    consent_file().write_text("{not json", encoding="utf-8")
    assert not upload_confirmed(AccountIdentity("work", "a@example.test"))


def test_browser_accounts_lists_emails(tmp_path: Path) -> None:
    payload = json.dumps({"accounts": [{"email": "a@example.test"}, {"email": "b@example.test"}]})
    process = FakeProcess(("auth inspect", 0, payload, ""))
    assert _client(tmp_path, process).browser_accounts("edge") == ["a@example.test", "b@example.test"]
    bad = FakeProcess(("auth inspect", 0, json.dumps({"nope": 1}), ""))
    with pytest.raises(NotebookLMError, match="unexpected shape"):
        _client(tmp_path, bad).browser_accounts("edge")


def test_an_unknown_account_is_never_recorded_or_confirmed() -> None:
    unknown = AccountIdentity("work", None)
    record_upload_consent(unknown)
    assert not consent_file().exists() and not upload_confirmed(unknown)


def test_a_recorded_unknown_account_does_not_count_as_consent() -> None:
    consent_file().parent.mkdir(parents=True, exist_ok=True)
    consent_file().write_text(json.dumps({"work": {"email": None}}), encoding="utf-8")
    assert not upload_confirmed(AccountIdentity("work", None))


def test_a_session_file_setting_overrides_the_profile_and_is_named(tmp_path: Path) -> None:
    storage = tmp_path / "my_session.json"
    storage.write_text("{}", encoding="utf-8")
    process = FakeProcess(("auth check", 0, _auth_check("me@example.test"), ""))
    client = _client(tmp_path, process, session_file=storage)
    identity = client.upload_account()
    assert identity.storage == str(storage) and client.session_file() == storage
    assert process.calls[0][:3] == ["notebooklm", "--storage", str(storage)]
    assert client.login_command()[:4] == ["notebooklm", "--storage", str(storage), "--profile"]


def test_an_explicit_profile_with_a_session_file_is_refused(tmp_path: Path) -> None:
    storage = tmp_path / "my_session.json"
    storage.write_text("{}", encoding="utf-8")
    process = FakeProcess(("list", 0, "[]", ""))
    client = _client(tmp_path, process, session_file=storage, notebooklm_profile="work")
    with pytest.raises(ConfigError, match="ALCHEMY_SESSION_FILE"):
        client.list_notebooks()
    assert process.calls == []  # refused before anything ran


def test_cli_version_comes_from_package_metadata_without_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess()
    client = _client(tmp_path, process)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert client.cli_version() is None
    monkeypatch.setattr("shutil.which", lambda name: "/bin/notebooklm")
    monkeypatch.setattr("importlib.metadata.version", lambda name: "0.8.6")
    assert client.cli_version() == "notebooklm-py 0.8.6" and process.calls == []

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr("shutil.which", lambda name: "/home/private-person/bin/notebooklm")
    monkeypatch.setattr("importlib.metadata.version", missing)
    shown = client.cli_version() or ""
    assert "not installed in this Python" in shown and "private-person" not in shown


def test_a_hand_edited_consent_record_asks_again_instead_of_crashing() -> None:
    consent_file().parent.mkdir(parents=True, exist_ok=True)
    consent_file().write_text(json.dumps({"salt": "s", "profiles": {"work": "\u00e9"}}), encoding="utf-8")
    assert not upload_confirmed(AccountIdentity("work", "a@example.test"))


def test_plan_limits_name_the_plan_and_are_never_guessed(tmp_path: Path) -> None:
    pro = PlanLimits(300, 500, 2)
    client = NotebookLMClient(AlchemyConfig(), limits_reader=lambda c: pro)
    assert client.plan_limits() == pro and pro.plan == "Pro"
    assert PlanLimits(50, 100, 1).plan == "Standard" and PlanLimits(None, None, 9).plan == "unknown plan"
    assert _client(tmp_path, FakeProcess()).plan_limits() is None  # a test runner never reads live
    assert NotebookLMClient(AlchemyConfig()).plan_limits() is None  # no session: unknown, not a guess


def test_notebooklm_may_renew_stale_cookies_from_its_own_sign_in_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, str]] = []

    def runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(dict(kwargs["env"]))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"notebooks": []}), "")

    client = _client(tmp_path, runner)  # type: ignore[arg-type]
    client.list_notebooks()
    assert seen[-1]["NOTEBOOKLM_HEADLESS_REAUTH"] == "1"
    monkeypatch.setenv("NOTEBOOKLM_HEADLESS_REAUTH", "0")  # the user's own choice is kept
    client.list_notebooks()
    assert seen[-1]["NOTEBOOKLM_HEADLESS_REAUTH"] == "0"
