"""Engine: uploads, the source limit, the NotebookLM transcript route and status."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from notebook_alchemy.client import NotebookLMClient, PlanLimits
from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.engine import AlchemyEngine
from notebook_alchemy.errors import SessionError, SyncError
from notebook_alchemy.library import RequestCache
from notebook_alchemy.writer import Book

from .conftest import FakeNotebookLM, FakeProcess


def _config(tmp_path: Path) -> AlchemyConfig:
    return AlchemyConfig(cache_dir=tmp_path / "cache", gemini_api_key="")


@pytest.fixture
def engine(tmp_path: Path) -> AlchemyEngine:
    return AlchemyEngine(_config(tmp_path))


def _session(tmp_path: Path) -> None:
    state = Path.home() / ".notebooklm" / "profiles" / "default" / "storage_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}", encoding="utf-8")


# --- distillation ---------------------------------------------------------------------------------


def test_status_reports_facts_without_network(engine: AlchemyEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(NotebookLMClient, "cli_version", lambda self: "notebooklm 9.9")
    facts = engine.status()
    assert facts["notebooklm_cli"] == "notebooklm 9.9"
    assert facts["gemini_api_key"] == "unset"
    assert facts["session_file"].startswith("none")
    assert "key" not in json.dumps(facts).lower().replace("gemini_api_key", "")  # no secret values


def _books(names: tuple[str, ...] = ("Alpha_Topics", "Beta_Topics")) -> list[Book]:
    """In-memory books titled like the old vault files' domains ("Alpha_Topics" -> "Alpha Topics")."""
    return [Book(n.replace("_", " "), f"# {n.replace('_', ' ')}\n\nwords") for n in names]


def _previous_upload(engine: AlchemyEngine, nb_id: str, titles: list[str]) -> None:
    """A record of an earlier (0.2-style, titles only) upload of these titles to the notebook."""
    engine._write_json(engine._sync_state_path(nb_id), {"titles": titles}, "test record")


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
    result = _fake_engine(tmp_path, fake).publish("Chan", _books())
    assert (result.created, result.uploaded, result.replaced) == (True, 2, 0)
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics"]


def test_uploads_and_waits_overlap_but_titles_are_fixed_only_after_all_waits(tmp_path: Path) -> None:
    fake = FakeNotebookLM(reset_titles=True)
    active, peak, order = [0], [0], []
    lock = threading.Lock()

    def slow(cmd: list[str], **kw: object):
        verb = cmd[cmd.index("source") + 1] if "source" in cmd else cmd[3]
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            order.append(verb)
        time.sleep(0.05 if verb in ("add", "wait") else 0)
        with lock:
            active[0] -= 1
        return fake(cmd, **kw)

    names = tuple(f"Topic_{i}" for i in range(6))
    result = _fake_engine(tmp_path, slow).publish("Chan", _books(names))  # type: ignore[arg-type]
    assert result.uploaded == 6 and fake.titles("Chan") == sorted(n.replace("_", " ") for n in names)
    assert peak[0] > 1  # several notebooklm processes ran at once
    assert max(i for i, v in enumerate(order) if v == "wait") < min(
        i for i, v in enumerate(order) if v == "rename"
    )


def test_an_unexpected_error_in_one_upload_is_reported_and_the_rest_reconcile(tmp_path: Path) -> None:
    fake = FakeNotebookLM()

    def flaky(cmd: list[str], **kw: object):
        if "add" in cmd and any("Beta" in part for part in cmd):
            raise RuntimeError("something nobody expected")
        return fake(cmd, **kw)

    with pytest.raises(SyncError) as err:
        _fake_engine(tmp_path, flaky).publish("Chan", _books())  # type: ignore[arg-type]
    assert any("Beta Topics" in f and "RuntimeError" in f for f in err.value.failed)
    assert fake.titles("Chan") == ["Alpha Topics"]  # the other upload was waited on and kept


def test_resync_replaces_old_copies_including_filename_titled_ones(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    nb = fake.seed("Chan", ["Alpha Topics", "Alpha Topics", "Beta_Topics.md", "My own notes"])
    engine = _fake_engine(tmp_path, fake)
    _previous_upload(engine, nb, ["Alpha Topics", "Beta Topics"])
    result = engine.publish("Chan", _books())
    assert result.replaced == 3  # both exact copies and the filename-titled one
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics", "My own notes"]  # user's own source kept


def test_titles_the_service_resets_to_file_names_are_corrected(tmp_path: Path) -> None:
    fake = FakeNotebookLM(reset_titles=True)  # the behaviour seen against the real NotebookLM
    engine = _fake_engine(tmp_path, fake)
    directory = _books()
    engine.publish("Chan", directory)
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics"]
    engine.publish("Chan", directory)  # a second run must not pile up duplicates
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics"]


def test_sync_adds_before_it_deletes_and_waits_before_renaming(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    nb = fake.seed("Chan", ["Alpha Topics"])
    engine = _fake_engine(tmp_path, fake)
    _previous_upload(engine, nb, ["Alpha Topics"])
    engine.publish("Chan", _books())
    verbs = [" ".join(c) for c in fake.calls]
    first_add = next(i for i, c in enumerate(verbs) if "source add" in c)
    first_wait = next(i for i, c in enumerate(verbs) if "source wait" in c)
    first_delete = next(i for i, c in enumerate(verbs) if "source delete" in c)
    assert first_add < first_wait < first_delete  # the old copy goes only after the new one is ready


def test_sync_reports_each_failed_file_and_still_uploads_the_rest(tmp_path: Path) -> None:
    fake = FakeNotebookLM(fail={"add": "Beta_Topics.md"})
    with pytest.raises(SyncError) as err:
        _fake_engine(tmp_path, fake).publish("Chan", _books())
    assert err.value.failed == ["Beta Topics: `notebooklm source add` failed: add failed on purpose"]
    assert fake.titles("Chan") == ["Alpha Topics"]


def test_an_old_copy_that_cannot_be_deleted_is_reported(tmp_path: Path) -> None:
    fake = FakeNotebookLM(fail={"delete": "old-"})
    nb = fake.seed("Chan", ["Alpha Topics"])
    engine = _fake_engine(tmp_path, fake)
    _previous_upload(engine, nb, ["Alpha Topics"])
    with pytest.raises(SyncError) as err:
        engine.publish("Chan", _books())
    assert any("old copy not removed" in f for f in err.value.failed)


def test_a_source_that_never_finishes_processing_is_reported(tmp_path: Path) -> None:
    fake = FakeNotebookLM(fail={"wait": "src-"})
    with pytest.raises(SyncError) as err:
        _fake_engine(tmp_path, fake).publish("Chan", _books())
    assert all("processing did not finish" in f for f in err.value.failed) and len(err.value.failed) == 2


def test_sync_needs_a_working_session(tmp_path: Path) -> None:
    engine = _sync_engine(tmp_path, FakeProcess())
    with pytest.raises(SessionError):
        engine.publish("Chan", _books())


# --- append ---------------------------------------------------------------------------------------


def test_no_gemini_client_without_a_key(engine: AlchemyEngine) -> None:
    assert engine.gemini is None


def test_a_later_run_with_different_domains_prunes_the_earlier_runs_sources(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    fake.seed("Chan", ["My own notes"])
    engine.publish("Chan", _books(("Alpha_Topics", "Beta_Topics")))
    second = engine.publish("Chan", _books(("Gamma_Topics", "Beta_Topics")))
    assert second.pruned == 1  # Alpha Topics is gone from this build
    assert fake.titles("Chan") == ["Beta Topics", "Gamma Topics", "My own notes"]


def test_nothing_is_pruned_without_a_record_of_an_earlier_sync(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    fake.seed("Chan", ["Old Domain", "My own notes"])
    result = _fake_engine(tmp_path, fake).publish("Chan", _books())
    assert result.pruned == 0 and "Old Domain" in fake.titles("Chan")


def test_a_failed_sync_does_not_overwrite_the_record(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    engine.publish("Chan", _books(("Alpha_Topics",)))
    broken = FakeNotebookLM(fail={"add": "Gamma"})
    broken.notebooks, broken.sources = fake.notebooks, fake.sources
    engine.client = NotebookLMClient(engine.config, runner=broken)
    with pytest.raises(SyncError):
        engine.publish("Chan", _books(("Gamma_Topics",)))
    engine.client = NotebookLMClient(engine.config, runner=fake)
    engine.publish("Chan", _books(("Gamma_Topics",)))
    assert fake.titles("Chan") == ["Gamma Topics"]  # Alpha was still recorded, so it is pruned now


# --- the account's source limit --------------------------------------------------------------------


def _limited_engine(tmp_path: Path, fake: FakeNotebookLM, limit: int | None) -> AlchemyEngine:
    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    plan = PlanLimits(limit, 100, 1) if limit is not None else None
    engine.client = NotebookLMClient(engine.config, runner=fake, limits_reader=lambda client: plan)
    return engine


def test_a_sync_that_cannot_fit_the_plan_uploads_nothing(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    fake.seed("Chan", ["Notes of my own", "Another note"])
    names = ("Alpha_Topics", "Beta_Topics", "Gamma_Topics")
    with pytest.raises(SyncError, match="would hold 5 sources") as err:
        _limited_engine(tmp_path, fake, 4).publish("Chan", _books(names))
    assert "Standard" in err.value.message and "holds up to 4" in err.value.message
    assert not any("add" in call for call in fake.calls)
    assert fake.titles("Chan") == ["Another note", "Notes of my own"]


def test_a_tight_notebook_frees_the_replaced_copies_before_uploading(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    nb = fake.seed("Chan", ["Alpha Topics", "Beta Topics", "Mine"])
    engine = _limited_engine(tmp_path, fake, 4)
    _previous_upload(engine, nb, ["Alpha Topics", "Beta Topics"])
    result = engine.publish("Chan", _books())
    assert result.freed_first and (result.uploaded, result.replaced) == (2, 2)
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics", "Mine"]
    verbs = [c[c.index("source") + 1] for c in fake.calls if "source" in c]
    assert verbs.index("delete") < verbs.index("add")  # a slot was freed before the first upload


def test_with_room_the_old_copies_go_only_after_the_new_ones_are_in(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    nb = fake.seed("Chan", ["Alpha Topics", "Beta Topics"])
    engine = _limited_engine(tmp_path, fake, 300)
    _previous_upload(engine, nb, ["Alpha Topics", "Beta Topics"])
    result = engine.publish("Chan", _books())
    assert not result.freed_first and result.source_limit == 300
    verbs = [c[c.index("source") + 1] for c in fake.calls if "source" in c]
    assert verbs.index("add") < verbs.index("delete")


def test_an_unknown_limit_is_not_guessed(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    result = _limited_engine(tmp_path, fake, None).publish("Chan", _books())
    assert result.source_limit is None and not result.freed_first and result.uploaded == 2


# --- transcripts through NotebookLM ----------------------------------------------------------------


def test_notebooklm_reads_blocked_videos_in_a_working_notebook_and_cleans_up(tmp_path: Path) -> None:
    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM()
    workspace = fake.seed("Notebook Alchemy workspace", ["left from an interrupted run"])
    # A source still being fetched has no URL yet: its title is the link.
    fake.sources[workspace].append(
        {
            "id": "yt-pending",
            "title": "https://www.youtube.com/watch?v=cdefghijklm",
            "url": None,
            "status": "ready",
        }
    )
    engine.client = NotebookLMClient(engine.config, runner=fake)
    texts = engine.transcripts_via_notebooklm(
        [("abcdefghijk", "One"), ("bcdefghijkl", "Two"), ("cdefghijklm", "Three")]
    )
    assert set(texts) == {"abcdefghijk", "bcdefghijkl", "cdefghijklm"}
    assert texts["abcdefghijk"] == "words spoken in https://www.youtube.com/watch?v=abcdefghijk"
    adds = [c for c in fake.calls if "add-async" in c]
    assert len(adds) == 1 and not any(
        "cdefghijklm" in a for a in adds[0]
    )  # the leftover was read, not re-added
    assert [s["title"] for s in fake.sources[workspace]] == ["left from an interrupted run"]  # not a video


def test_notebooklm_route_needs_a_sign_in(tmp_path: Path) -> None:
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM()
    engine.client = NotebookLMClient(engine.config, runner=fake)
    assert engine.transcripts_via_notebooklm([("abcdefghijk", "One")]) == {} and fake.calls == []


# --- the whole pipeline ----------------------------------------------------------------------------


class PipelineGemini:
    """Answers every request the pipeline makes: labels, the topics, placement, consolidation."""

    model = "gemini-test"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, **kw):
        import re as _re

        from notebook_alchemy.gemini import GeminiResult

        self.prompts.append(prompt)
        if prompt.startswith("Video:"):
            nums = [int(n) for n in _re.findall(r"^\[(\d+)\]", prompt, _re.MULTILINE)]
            segs = [
                {"from": nums[0], "to": nums[-1], "keep": True, "headline": "Registering", "kind": "rule"}
            ]
            return GeminiResult(json.dumps({"segments": segs}), "STOP")
        if "study library" in prompt:
            topics = [
                {
                    "name": "Getting Registered",
                    "sections": [{"name": "SAM.gov", "about": "the federal register"}],
                }
            ]
            return GeminiResult(json.dumps({"notebook_title": "GovCon Basics", "topics": topics}), "STOP")
        if "Place each numbered passage" in prompt:
            nums = _re.findall(r"^(\d+): ", prompt, _re.MULTILINE)
            return GeminiResult(json.dumps({"assignments": {n: 0 for n in nums}}), "STOP")
        nums = [int(n) for n in _re.findall(r"^(\d+): \[", prompt, _re.MULTILINE)]
        return GeminiResult(json.dumps({"intro": "How to register.", "order": nums}), "STOP")

    def submit_batch(self, requests, name):
        from notebook_alchemy.errors import GeminiError

        raise GeminiError("refused", status=400, detail="Precondition check failed.")

    @staticmethod
    def batch_refused(exc) -> bool:
        return True

    def usage(self):
        return []


def test_a_channel_becomes_topic_books_and_a_rerun_asks_gemini_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_alchemy.channel import (
        ChannelScanner,
        ChannelScanResult,
        ChannelVideo,
        TranscriptManager,
        TranscriptResult,
    )

    videos = [ChannelVideo(f"vid0000000{i}", f"SAM.gov part {i}", "u", 600, "2026-03-01") for i in range(3)]
    monkeypatch.setattr(
        ChannelScanner, "scan", lambda self, url, limit=None, **kw: ChannelScanResult("Chan", videos)
    )
    words = (
        "Welcome back to the channel. Registering in SAM.gov takes ten days. You need your EIN exactly as "
        "the IRS shows it."
    )
    monkeypatch.setattr(
        TranscriptManager,
        "fetch_many",
        lambda self, items, progress=None: {
            v: TranscriptResult(v, f"{words} Video {v} adds its own fact.", "youtube") for v, _ in items
        },
    )
    engine = AlchemyEngine(_config(tmp_path))
    engine.gemini = PipelineGemini()  # type: ignore[assignment]
    report = engine.build("@chan")
    assert report.library.notebook_title == "GovCon Basics" and report.used_ai
    book = next(b for b in report.books if b.title == "Getting Registered")
    assert book.content.count("takes ten days") == 1  # said in all three videos, kept once
    assert all(f"vid0000000{i} adds its own fact" in book.content for i in range(3))  # what is new stays
    assert "Welcome back" not in book.content and not book.content.startswith("---")
    asked = len(engine.gemini.prompts)
    engine.build("@chan")
    assert len(engine.gemini.prompts) == asked  # labels, topics, placement and sections all cached
    stored = RequestCache(engine.library_cache_path("Chan")).get("parts")
    assert stored and all(part == 1 for sections in stored.values() for part in sections.values())


def test_a_rerun_updates_the_same_notebook_even_when_the_title_changes(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    first = engine.publish("First Title", _books(), channel_key="@chan")
    second = engine.publish("A Better Title", _books(), channel_key="@chan")
    assert second.notebook_id == first.notebook_id and not second.created
    assert [n["title"] for n in fake.notebooks] == ["First Title"]


def test_the_estimate_counts_cached_words_and_spends_nothing(tmp_path: Path) -> None:
    from notebook_alchemy.channel import ChannelVideo

    engine = AlchemyEngine(_config(tmp_path))
    engine.transcripts._write_cache("vid00000001", "t", "word " * 1000, method="youtube")
    est = engine.estimate(
        [ChannelVideo("vid00000001", "t", "u", 600), ChannelVideo("vid00000002", "t", "u", 600)]
    )
    assert est.videos_known == 1 and est.words_known == 1000
    assert est.words_estimated == 1000 + int(600 * 2.5) and est.cost_usd > 0


def test_a_used_up_quota_while_sorting_says_how_far_it_got_and_keeps_the_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_alchemy.channel import (
        ChannelScanner,
        ChannelScanResult,
        ChannelVideo,
        TranscriptManager,
        TranscriptResult,
    )
    from notebook_alchemy.errors import AlchemyError, GeminiError

    videos = [ChannelVideo("vid00000001", "One", "u", 600, "2026-03-01")]
    monkeypatch.setattr(
        ChannelScanner, "scan", lambda self, url, limit=None, **kw: ChannelScanResult("Chan", videos)
    )
    monkeypatch.setattr(
        TranscriptManager,
        "fetch_many",
        lambda self, items, progress=None: {
            v: TranscriptResult(v, "Registering takes ten days.", "youtube") for v, _ in items
        },
    )

    class QuotaWhenSorting(PipelineGemini):
        def generate(self, prompt: str, **kw):
            if "study library" in prompt:
                raise GeminiError("quota", status=429, detail="daily request quota exhausted", quota=True)
            return super().generate(prompt, **kw)

    engine = AlchemyEngine(_config(tmp_path))
    engine.gemini = QuotaWhenSorting()  # type: ignore[assignment]
    with pytest.raises(AlchemyError, match="1 of 1 videos are labelled and saved") as err:
        engine.build("@chan")
    assert "carries on from here" in (err.value.hint or "")
    assert list((tmp_path / "knowledge").glob("*.json"))  # the label survived for the next run


def test_a_source_of_yours_with_a_books_title_is_left_alone(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    fake.seed("Chan", ["Alpha Topics", "My own notes"])  # no record: these are not the tool's
    result = _fake_engine(tmp_path, fake).publish("Chan", _books())
    assert result.replaced == 0 and fake.titles("Chan").count("Alpha Topics") == 2


def test_a_book_that_fails_to_upload_keeps_its_old_copy(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    engine.publish("Chan", _books())  # first run: both books, recorded by id
    broken = FakeNotebookLM(fail={"add": "Beta"})
    broken.notebooks, broken.sources = fake.notebooks, fake.sources
    engine.client = NotebookLMClient(engine.config, runner=broken)
    with pytest.raises(SyncError):
        engine.publish("Chan", _edited(_books()))
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics"]  # Beta's old copy is still there
    engine.client = NotebookLMClient(engine.config, runner=fake)
    result = engine.publish("Chan", _edited(_books()))
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics"]  # and the next run replaces it once
    assert (result.uploaded, result.unchanged) == (1, 1)  # Beta was still the old text; Alpha was in


def _edited(books: list[Book]) -> list[Book]:
    return [Book(b.title, b.content + " and more") for b in books]


def test_only_books_whose_text_changed_are_sent_again(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    engine.publish("Chan", _books())
    ids = {s["title"]: s["id"] for s in fake.sources[fake.notebook_id("Chan")]}
    again = engine.publish("Chan", _books())
    assert (again.uploaded, again.replaced, again.unchanged) == (0, 0, 2)
    assert {s["title"]: s["id"] for s in fake.sources[fake.notebook_id("Chan")]} == ids
    alpha, beta = _books()
    one = engine.publish("Chan", [alpha, *_edited([beta])])
    assert (one.uploaded, one.replaced, one.unchanged) == (1, 1, 1)
    now = {s["title"]: s["id"] for s in fake.sources[fake.notebook_id("Chan")]}
    assert now["Alpha Topics"] == ids["Alpha Topics"] and now["Beta Topics"] != ids["Beta Topics"]


def test_an_unchanged_book_removed_in_notebooklm_is_sent_again(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    engine.publish("Chan", _books())
    nb = fake.notebook_id("Chan")
    fake.sources[nb] = [s for s in fake.sources[nb] if s["title"] != "Alpha Topics"]
    result = engine.publish("Chan", _books())
    assert (result.uploaded, result.unchanged) == (1, 1) and fake.titles("Chan") == [
        "Alpha Topics",
        "Beta Topics",
    ]


def test_the_working_notebook_only_ever_loses_videos_and_titles_must_be_unique(tmp_path: Path) -> None:
    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM()
    ws = fake.seed("Notebook Alchemy workspace", ["My notes"])
    fake.sources[ws].append(
        {"id": "yt-old", "title": "Live Q&A", "url": None, "type": "youtube", "status": "ready"}
    )
    engine.client = NotebookLMClient(engine.config, runner=fake)
    texts = engine.transcripts_via_notebooklm([("abcdefghijk", "Live Q&A"), ("bcdefghijkl", "Live Q&A")])
    # The leftover titled "Live Q&A" matches two queued videos, so it is read for neither: each video's
    # words come from its own URL.
    assert texts["abcdefghijk"] == "words spoken in https://www.youtube.com/watch?v=abcdefghijk"
    assert texts["bcdefghijkl"] == "words spoken in https://www.youtube.com/watch?v=bcdefghijkl"
    assert [s["title"] for s in fake.sources[ws]] == ["My notes"]
    assert engine.notebook_for("__workspace__") == ws
