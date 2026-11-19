"""NotebookLM client: session detection and upload behaviour."""

from __future__ import annotations

import importlib.metadata
import json
import threading
from pathlib import Path
from types import SimpleNamespace

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

from .conftest import FakeNotebookLM, FakeProcess, _FakeContext


def _client(tmp_path: Path, process: FakeProcess | None = None, *, api=None, **cfg) -> NotebookLMClient:
    config = AlchemyConfig(cache_dir=tmp_path / "c", **cfg)
    return NotebookLMClient(config, runner=process, api=api)


def _two_notebooks() -> FakeNotebookLM:
    fake = FakeNotebookLM()
    fake.seed("Alpha", [])
    fake.seed("Alpha Beta", [])
    return fake


def _raising(error: Exception) -> FakeNotebookLM:
    """A NotebookLM that answers every request with this error."""
    fake = FakeNotebookLM()

    def refuse(words: list[str]) -> None:
        raise error

    fake.before = refuse
    return fake


def _keep_library_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real opener changes the library's logger; put it back when the test ends."""
    import logging

    library = logging.getLogger("notebooklm")
    monkeypatch.setattr(library, "propagate", library.propagate)
    monkeypatch.setattr(library, "level", library.level)


def _library(**parts: object):
    """A hand-made library, for answers the in-memory fake would never give."""
    return lambda profile, path: _FakeContext(SimpleNamespace(**parts))


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

    good = _client(tmp_path, api=_two_notebooks())
    _profile_session(good)
    status = good.check_session()
    assert status.ok and status.notebooks == 2

    expired = _client(tmp_path, api=_raising(FakeNotebookLM.sign_in_ended()))
    result = expired.check_session()
    assert not result.ok and "expired" in result.detail.lower()
    assert missing.signed_out  # no sign-in at all
    _profile_session(expired)
    assert expired.check_session().signed_out  # an ended sign-in


def test_upload_sends_a_file_not_content_and_not_text_flag(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    nb = fake.seed("Chan", [])
    client = _client(tmp_path, api=fake)
    big = "x" * 60_000  # larger than one command-line argument may be on Windows
    src_id = client.add_source_document(nb, SourceDocument("a", "Big Doc", big))
    assert src_id.startswith("src-") and fake.uploaded == {"Big Doc": big}
    sent = next(c for c in fake.calls if "add" in c)
    assert sent[3] == "Big_Doc.md" and big not in sent  # a markdown file is sent, named for the title


def test_the_real_library_accepts_how_it_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real notebooklm-py client is built, offline, from the tool's own opener.

    Every other test replaces the opener, so only this one sees an argument the library rejects or
    has deprecated. The stored sign-in is replaced by synthetic tokens; nothing is sent anywhere.
    """
    import asyncio
    import warnings

    from notebooklm import client as library
    from notebooklm._auth import tokens

    from .conftest import real_default_api

    asked: dict[str, object] = {}
    auth = library.AuthTokens(
        cookies={"SID": "synthetic-test"}, csrf_token="synthetic", session_id="synthetic"
    )

    async def stored(**kwargs: object):
        asked.update(kwargs)
        return tokens.InlineLoadedAuth(auth=auth)

    monkeypatch.setattr(tokens, "_load_stored_auth", stored)
    _keep_library_logging(monkeypatch)
    with warnings.catch_warnings():
        # A deprecated argument is a failure here; other packages' warnings are not this test's.
        warnings.filterwarnings("error", message=".*NotebookLMClient.*", category=DeprecationWarning)
        built = asyncio.run(real_default_api()("work", None)._build())
    assert type(built).__name__ == "NotebookLMClient" and asked["profile"] == "work" and asked["path"] is None
    # No hidden-browser renewal: it made a signed-out check take minutes.
    assert asked["policy"].allow_headless is False  # type: ignore[attr-defined]
    for part in ("notebooks", "sources", "settings"):
        assert hasattr(built, part)


def test_an_upload_gets_a_longer_wait_than_the_librarys_usual_one(monkeypatch: pytest.MonkeyPatch) -> None:
    import notebooklm

    from .conftest import real_default_api

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        notebooklm.NotebookLMClient, "from_storage", classmethod(lambda cls, **kw: seen.update(kw))
    )
    _keep_library_logging(monkeypatch)
    real_default_api()(None, "session.json")
    assert seen["path"] == "session.json" and seen["profile"] is None and seen["allow_headless"] is False
    transfers = seen["config"].transfers  # type: ignore[attr-defined]
    assert transfers.start_timeout.read == 180.0
    assert transfers.finalize_timeout is None  # sending the file keeps the library's own, longer wait


def test_an_upload_without_a_source_id_is_an_error(tmp_path: Path) -> None:
    class Sources:
        async def add_file(self, *a: object, **k: object) -> SimpleNamespace:
            return SimpleNamespace(id="", title="Doc")

    client = _client(tmp_path, api=_library(sources=Sources()))
    with pytest.raises(NotebookLMError, match="no source id"):
        client.add_source_document("nb-1", SourceDocument("a", "Doc", "body"))


def test_upload_failure_raises_with_cause(tmp_path: Path) -> None:
    import notebooklm.exceptions as lib

    fake = FakeNotebookLM(fail={"add": "Doc"})
    nb = fake.seed("Chan", [])
    with pytest.raises(NotebookLMError) as err:
        _client(tmp_path, api=fake).add_source_document(nb, SourceDocument("a", "Doc", "body"))
    assert "add failed on purpose" in err.value.message and "upload 'Doc'" in err.value.message
    assert isinstance(err.value.__cause__, lib.NotebookLMError) and err.value.hint


def test_a_slow_answer_is_a_typed_error_and_the_call_has_let_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("notebook_alchemy.client.LIST_TIMEOUT", 0.05)
    fake = FakeNotebookLM(delay={"list": 5})
    with pytest.raises(NotebookLMError, match="did not finish") as slow:
        _client(tmp_path, api=fake).list_notebooks()
    assert not isinstance(slow.value, SessionError) and "connection" in (slow.value.hint or "")
    assert fake.active == 0  # the request was stopped and had ended before the error was raised


def test_only_a_sign_in_file_problem_is_a_sign_out_when_opening(tmp_path: Path) -> None:
    import httpx

    def opener(error: Exception):
        def refuse(profile: str | None, path: str | None) -> None:
            raise error

        return refuse

    class RequiredCookieValidationError(ValueError):  # the library's name for cookies Google needs
        pass

    class StorageStateValidationError(ValueError):  # its name for a file in the wrong shape
        pass

    for error, words in (
        (FileNotFoundError("storage_state.json"), "missing"),
        (json.JSONDecodeError("not JSON", "{", 0), "unusable"),
        (RequiredCookieValidationError("no SID cookie; try --browser-cookies"), "unusable"),
        (StorageStateValidationError("Storage state must contain a 'cookies' list."), "unusable"),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), "unusable"),
        (PermissionError("storage_state.json"), "unusable"),
    ):
        with pytest.raises(SessionError) as gone:
            _client(tmp_path, api=opener(error)).list_notebooks()
        assert words in gone.value.message and "notebook-alchemy login" in (gone.value.hint or "")
        assert "--browser-cookies" not in gone.value.message  # a route this tool removed

    # A page the library cannot read, or a region block: signing in again would not help.
    with pytest.raises(NotebookLMError) as odd:
        _client(tmp_path, api=opener(ValueError("the page shape changed"))).list_notebooks()
    assert not isinstance(odd.value, SessionError) and "installer again" in (odd.value.hint or "")

    # No connection while opening: a network fault, never a sign-out.
    offline = _client(tmp_path, api=opener(httpx.ConnectError("no route to host")))
    with pytest.raises(NotebookLMError, match="could not be reached") as down:
        offline.list_notebooks()
    assert not isinstance(down.value, SessionError)
    _profile_session(offline)
    status = offline.check_session()
    assert not status.ok and not status.signed_out
    request = httpx.Request("GET", "https://notebooklm.example.test/")
    busy = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    with pytest.raises(NotebookLMError, match="limiting requests"):
        _client(tmp_path, api=opener(busy)).list_notebooks()


def test_closing_while_a_request_is_under_way_ends_it_at_once(tmp_path: Path) -> None:
    import time

    fake = _two_notebooks()
    fake.delay = {"list": 30}
    client = _client(tmp_path, api=fake)
    seen: list[BaseException] = []

    def ask() -> None:
        try:
            client.list_notebooks()
        except BaseException as exc:
            seen.append(exc)

    worker = threading.Thread(target=ask)
    worker.start()
    while fake.active == 0 and worker.is_alive():
        time.sleep(0.01)
    started = time.monotonic()
    client.close()
    worker.join(10)
    assert not worker.is_alive() and time.monotonic() - started < 10  # not the 30 s the request wanted
    assert isinstance(seen[0], NotebookLMError) and "closed before" in seen[0].message
    fake.delay = {}
    assert len(client.list_notebooks()) == 2 and len(fake.opened) == 2  # a new session, tracked


def test_a_source_registered_before_a_failed_upload_is_named_on_the_error(tmp_path: Path) -> None:
    import notebooklm.exceptions as lib

    def failing(error: Exception) -> NotebookLMClient:
        fake = _raising(error)
        fake.seed("Chan", [])
        return _client(tmp_path, api=fake)

    document = SourceDocument("a", "Doc", "body")
    with pytest.raises(NotebookLMError) as err:
        failing(lib.SourceProcessingError("src-half")).add_source_document("nb-1", document)
    assert err.value.source_id == "src-half"
    with pytest.raises(NotebookLMError) as plain:
        failing(lib.ServerError("HTTP 500")).add_source_document("nb-1", document)
    assert plain.value.source_id is None
    # An id on any other call's error is not a source this tool made.
    with pytest.raises(NotebookLMError) as other:
        failing(lib.SourceNotFoundError("someone-elses")).delete_source("nb-1", "someone-elses")
    assert other.value.source_id is None


def _library_errors() -> list[tuple[Exception, type[NotebookLMError], str]]:
    import notebooklm.exceptions as lib

    return [
        (lib.AuthError("Authentication expired or invalid."), SessionError, "rejected the session"),
        (lib.RateLimitError("slow down", retry_after=30), NotebookLMError, "asks for 30 seconds"),
        (lib.NotebookLimitError(100, limit=100), NotebookLMError, "as many notebooks as its plan allows"),
        (lib.SourceTimeoutError("s-1", 1.0), NotebookLMError, "still processing"),
        (TimeoutError("timed out"), NotebookLMError, "could not be reached"),
        (lib.SourceProcessingError("s-1"), NotebookLMError, "could not process"),
        (lib.SourceNotFoundError("s-1"), NotebookLMError, "no longer in the notebook"),
        (lib.NetworkError("connection reset"), NotebookLMError, "could not be reached"),
        (lib.RPCTimeoutError("no answer in 30s"), NotebookLMError, "could not be reached"),
        (lib.DecodingError("unexpected shape"), NotebookLMError, "cannot read"),
        (lib.ServerError("HTTP 500"), NotebookLMError, "refused"),
    ]


def test_each_library_error_becomes_the_right_typed_error_by_its_type(tmp_path: Path) -> None:
    for raised, expected, words in _library_errors():
        with pytest.raises(NotebookLMError) as err:
            _client(tmp_path, api=_raising(raised)).list_notebooks()
        assert type(err.value) is expected, raised
        assert words in err.value.message and err.value.hint, raised
        assert err.value.__cause__ is raised


def test_a_video_that_mentions_signing_in_is_not_a_sign_out(tmp_path: Path) -> None:
    # Errors are told apart by type. Matching words made any message with "login" in it a sign-out.
    import notebooklm.exceptions as lib

    said = lib.SourceProcessingError("s-1", message="This video requires login to view.")
    with pytest.raises(NotebookLMError) as err:
        _client(tmp_path, api=_raising(said)).list_notebooks()
    assert not isinstance(err.value, SessionError)


def test_an_error_that_is_not_notebooklms_is_not_dressed_up(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="a bug"):
        _client(tmp_path, api=_raising(RuntimeError("a bug"))).list_notebooks()


def test_an_answer_the_library_cannot_read_points_to_an_update(tmp_path: Path) -> None:
    import notebooklm.exceptions as lib

    with pytest.raises(NotebookLMError) as err:
        _client(tmp_path, api=_raising(lib.DecodingError("unexpected shape"))).list_notebooks()
    assert "installer again" in (err.value.hint or "")


def test_auth_failure_is_a_session_error(tmp_path: Path) -> None:
    client = _client(tmp_path, api=_raising(FakeNotebookLM.sign_in_ended()))
    with pytest.raises(SessionError):
        client.list_notebooks()


def test_find_notebook_exact_then_unique_partial_and_ambiguity(tmp_path: Path) -> None:
    fake = _two_notebooks()
    client = _client(tmp_path, api=fake)
    first, second = fake.notebook_id("Alpha"), fake.notebook_id("Alpha Beta")
    assert client.find_notebook("alpha")["id"] == first  # exact title wins over the longer title
    assert client.find_notebook(second)["title"] == "Alpha Beta"
    assert client.find_notebook("beta")["id"] == second
    assert client.find_notebook("zzz") is None
    data = FakeNotebookLM()
    data.seed("Data A", [])
    data.seed("Data B", [])
    with pytest.raises(NotebookLMError, match="matches 2 notebooks"):
        _client(tmp_path, api=data).find_notebook("data")


def test_create_notebook_reuses_exact_title_only(tmp_path: Path) -> None:
    fake = _two_notebooks()
    client = _client(tmp_path, api=fake)
    assert client.create_notebook("Alpha") == (fake.notebook_id("Alpha"), False)  # exact title reused
    made, created = client.create_notebook("Alph")  # a prefix is not a match
    assert created and made == fake.notebook_id("Alph")
    assert ["create", "Alph"] in fake.calls


def test_create_without_id_is_an_error(tmp_path: Path) -> None:
    class Notebooks:
        async def list(self) -> list[SimpleNamespace]:
            return []

        async def create(self, title: str) -> SimpleNamespace:
            return SimpleNamespace(id="", title=title)

    client = _client(tmp_path, api=_library(notebooks=Notebooks()))
    with pytest.raises(NotebookLMError, match="no notebook id"):
        client.create_notebook("New")


def test_a_session_file_setting_that_does_not_exist_is_ignored(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    missing = tmp_path / "old-env" / "session.json"  # for example copied from an old .env
    _client(tmp_path, api=fake, session_file=missing).list_notebooks()
    assert fake.opened == [("default", None)]  # the profile's own sign-in


def test_an_existing_session_file_setting_opens_that_file(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    session.write_text("{}", encoding="utf-8")
    fake = FakeNotebookLM()
    _client(tmp_path, api=fake, session_file=session).list_notebooks()
    assert fake.opened == [(None, str(session))]


def test_one_session_is_opened_on_first_use_and_shared_by_every_thread(tmp_path: Path) -> None:
    fake = _two_notebooks()
    client = _client(tmp_path, api=fake)
    assert fake.opened == [] and not client.session_is_open  # nothing is opened until it is needed
    threads = [threading.Thread(target=client.list_notebooks) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(fake.opened) == 1 and len(fake.calls) == 6 and fake.threads == {"notebooklm-session"}


def test_closing_stops_the_background_thread_and_the_next_call_opens_again(tmp_path: Path) -> None:
    def session_threads() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name == "notebooklm-session"]

    fake = _two_notebooks()
    client = _client(tmp_path, api=fake)
    client.list_notebooks()
    assert client.session_is_open and len(session_threads()) == 1
    client.close()
    client.close()  # closing twice is harmless
    assert not client.session_is_open and session_threads() == []
    assert len(client.list_notebooks()) == 2 and len(fake.opened) == 2
    from notebook_alchemy.client import close_all

    close_all()
    assert session_threads() == []


def test_a_failed_open_leaves_no_thread_behind(tmp_path: Path) -> None:
    def no_file(profile: str | None, path: str | None) -> None:
        raise FileNotFoundError("storage_state.json")

    client = _client(tmp_path, api=no_file)
    with pytest.raises(SessionError):
        client.list_notebooks()
    assert not [t for t in threading.enumerate() if t.name == "notebooklm-session"]


def test_sources_are_plain_records_with_the_fields_the_tool_reads(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    nb = fake.seed("Chan", ["A book"])
    client = _client(tmp_path, api=fake)
    client.add_youtube_sources(nb, ["https://www.youtube.com/watch?v=vid00000001"])
    book, video = client.list_sources(nb)
    assert book == {"id": book["id"], "title": "A book", "url": "", "type": "markdown", "status": "ready"}
    assert video["type"] == "youtube" and video["url"].endswith("vid00000001")
    assert client.source_text(nb, video["id"]).startswith("words spoken in")
    client.rename_source(nb, book["id"], "Renamed")
    client.wait_for_source(nb, book["id"])
    client.delete_source(nb, video["id"])
    assert fake.titles("Chan") == ["Renamed"]


def test_the_sign_in_file_is_found_where_notebooklm_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, notebooklm_profile="work")
    assert (
        client.profile_state_path()
        == Path.home() / ".notebooklm" / "profiles" / "work" / "storage_state.json"
    )
    moved = tmp_path / "elsewhere"
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(moved))
    assert client.profile_state_path() == moved.resolve() / "profiles" / "work" / "storage_state.json"
    # The default profile falls back to the older file in the folder's root, as notebooklm does.
    default = _client(tmp_path)
    moved.mkdir()
    (moved / "storage_state.json").write_text("{}", encoding="utf-8")
    assert default.profile_state_path() == moved.resolve() / "storage_state.json"
    assert client.profile_state_path().parent.name == "work"  # a named profile never falls back


def test_check_session_reports_any_failure_instead_of_raising(tmp_path: Path) -> None:
    import notebooklm.exceptions as lib

    client = _client(tmp_path, api=_raising(lib.ServerError("something unexpected broke")))
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
    fake = FakeNotebookLM()
    _client(tmp_path, api=fake, notebooklm_profile="work").list_notebooks()
    assert fake.opened[-1] == ("work", None)
    config = AlchemyConfig(notebooklm_profile="work")
    NotebookLMClient(config, "other", api=fake).list_notebooks()
    assert fake.opened[-1] == ("other", None)


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
    fake = FakeNotebookLM()
    client = _client(tmp_path, api=fake, session_file=storage, notebooklm_profile="work")
    with pytest.raises(ConfigError, match="ALCHEMY_SESSION_FILE"):
        client.list_notebooks()
    assert fake.opened == [] and fake.calls == []  # refused before anything was opened
    with pytest.raises(ConfigError, match="ALCHEMY_SESSION_FILE"):
        client.login_command()


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


def test_plan_limits_name_the_plan_and_are_never_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pro = PlanLimits(300, 500, 2)
    client = NotebookLMClient(AlchemyConfig(), limits_reader=lambda c: pro)
    assert client.plan_limits() == pro and pro.plan == "Pro"
    assert PlanLimits(50, 100, 1).plan == "Standard" and PlanLimits(None, None, 9).plan == "unknown plan"
    fake = FakeNotebookLM()
    assert _client(tmp_path, FakeProcess()).plan_limits() is None  # a test double never reads live
    assert NotebookLMClient(AlchemyConfig()).plan_limits() is None  # no session: unknown, not a guess
    doubled = _client(tmp_path, api=fake)
    _profile_session(doubled)  # signed in, so only the double stops a live read
    monkeypatch.setattr(NotebookLMClient, "_read_plan_limits_live", lambda self: pro)
    assert doubled.plan_limits() is None and fake.opened == []


def test_the_test_suite_refuses_every_connection_except_a_socket_pair() -> None:
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        for address in (("192.0.2.1", 9), listener.getsockname()):  # a reserved address; this computer
            with socket.socket() as stray:
                stray.settimeout(0.2)
                with pytest.raises(AssertionError, match="real network"):
                    stray.connect(address)
    one, other = socket.socketpair()  # how an event loop wakes itself
    with one, other:
        one.sendall(b"x")
        assert other.recv(1) == b"x"


def test_a_closed_session_is_never_reopened_behind_the_clients_back() -> None:
    from notebook_alchemy import client as client_module

    fake = FakeNotebookLM()
    session = client_module._Session(lambda: fake("work", None))

    async def ask(library: SimpleNamespace) -> int:
        return len(await library.notebooks.list())

    assert session.run(ask, 5) == 0 and len(fake.opened) == 1
    session.close()
    # A thread that still holds the closed session gets an error; it does not open one the
    # client no longer tracks or closes.
    with pytest.raises(client_module._Closed):
        session.run(ask, 5)
    assert len(fake.opened) == 1 and not session.is_open


def test_a_client_whose_session_was_closed_elsewhere_opens_a_new_one(tmp_path: Path) -> None:
    from notebook_alchemy.client import close_all

    fake = _two_notebooks()
    client = _client(tmp_path, api=fake)
    client.list_notebooks()
    close_all()  # what the end of every command does
    assert not client.session_is_open
    assert len(client.list_notebooks()) == 2 and len(fake.opened) == 2


def test_the_librarys_own_warnings_are_shown_once_and_only_with_v(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    import notebooklm

    from .conftest import real_default_api

    library, root = logging.getLogger("notebooklm"), logging.getLogger()
    monkeypatch.setattr(library, "propagate", True)
    monkeypatch.setattr(library, "level", logging.WARNING)
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.setattr(notebooklm.NotebookLMClient, "from_storage", classmethod(lambda cls, **kw: None))
    real_default_api()("work", None)
    assert library.propagate is False and library.level == logging.ERROR  # quiet by default, never twice
    monkeypatch.setattr(library, "level", logging.WARNING)
    monkeypatch.setattr(root, "level", logging.INFO)  # -v
    real_default_api()("work", None)
    assert library.level == logging.WARNING
    monkeypatch.setattr(root, "level", logging.DEBUG)  # -vv
    real_default_api()("work", None)
    assert library.level == logging.DEBUG


def test_an_answer_that_arrives_just_after_the_time_ran_out_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import contextlib

    class Notebooks:
        async def list(self) -> list[SimpleNamespace]:
            # Told to stop, the library still finishes what NotebookLM already has.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(30)
            return [SimpleNamespace(id="nb-late", title="Late")]

    monkeypatch.setattr("notebook_alchemy.client.LIST_TIMEOUT", 0.05)
    client = _client(tmp_path, api=_library(notebooks=Notebooks()))
    assert client.list_notebooks() == [{"id": "nb-late", "title": "Late"}]


def test_closing_frees_a_request_that_will_not_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import time

    started = threading.Event()

    class Notebooks:
        async def list(self) -> list[SimpleNamespace]:
            started.set()
            while True:  # ignores being told to stop
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    continue

    monkeypatch.setattr("notebook_alchemy.client.CLOSE_TIMEOUT", 0.2)
    client = _client(tmp_path, api=_library(notebooks=Notebooks()))
    seen: list[BaseException] = []

    def ask() -> None:
        try:
            client.list_notebooks()
        except BaseException as exc:
            seen.append(exc)

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    assert started.wait(5)
    began = time.monotonic()
    client.close()
    worker.join(10)
    assert not worker.is_alive() and time.monotonic() - began < 10  # not the 60 s it would wait
    assert isinstance(seen[0], NotebookLMError) and "closed before" in seen[0].message


def test_ctrl_c_while_waiting_tells_the_request_to_stop(tmp_path: Path) -> None:
    from notebook_alchemy import client as client_module

    started = threading.Event()
    fake = FakeNotebookLM(delay={"list": 30})
    real_pause = fake.pause

    async def tracked_pause(verb: str) -> None:
        if verb == "list":
            started.set()
        await real_pause(verb)

    fake.pause = tracked_pause  # type: ignore[method-assign]
    session = client_module._Session(lambda: fake("work", None))

    async def ask(library: SimpleNamespace) -> int:
        return len(await library.notebooks.list())

    class Interrupted(client_module._Pending):
        def __init__(self) -> None:
            super().__init__()

            def wait(timeout: float | None = None) -> bool:
                assert started.wait(5)  # wait until the request is actively under way on the loop
                raise KeyboardInterrupt

            self.done.wait = wait  # type: ignore[method-assign]

    session._open()  # opened first, so the interruption lands on the request, not on opening
    original = client_module._Pending
    client_module._Pending = Interrupted  # type: ignore[misc]
    try:
        with pytest.raises(KeyboardInterrupt):
            session.run(ask, 60)
    finally:
        client_module._Pending = original  # type: ignore[misc]
    assert fake.calls == [["list"]] or fake.active == 1  # the request had started
    for _ in range(100):
        if fake.active == 0:
            break
        threading.Event().wait(0.05)
    assert fake.active == 0  # stopped, not left running for its 30 s
    session.close()


def test_a_call_that_arrives_as_the_session_closes_is_refused() -> None:
    from notebook_alchemy import client as client_module

    fake = FakeNotebookLM()
    session = client_module._Session(lambda: fake("work", None))

    async def ask(library: SimpleNamespace) -> int:
        return len(await library.notebooks.list())

    opened = session._open()
    session.close()
    session._open = lambda: opened  # type: ignore[method-assign]  # it got in just before the close
    with pytest.raises(client_module._Closed):
        session.run(ask, 5)
    assert fake.calls == []  # nothing was started on the stopped loop
