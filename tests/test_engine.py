"""Engine: uploads, the source limit, the NotebookLM transcript route and status."""

from __future__ import annotations

import json
import subprocess
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
    return AlchemyConfig(cache_dir=tmp_path / "cache", api_key="")


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
    assert facts["api_key"] == "unset"
    assert facts["session_file"].startswith("none")
    assert "key" not in json.dumps(facts).lower().replace("api_key", "")  # no secret values


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


def test_no_ai_client_without_a_key(engine: AlchemyEngine) -> None:
    assert engine.ai is None


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


class PipelineAI:
    """Answers every request the pipeline makes: labels, the topics, placement, consolidation."""

    model = "claude-test"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, schema: dict, **kw):
        import re as _re

        from notebook_alchemy.claude import ClaudeResult

        self.prompts.append(prompt)
        if prompt.startswith("Video:"):
            nums = [int(n) for n in _re.findall(r"^\[(\d+)\]", prompt, _re.MULTILINE)]
            segs = [
                {"from": nums[0], "to": nums[-1], "keep": True, "headline": "Registering", "kind": "rule"}
            ]
            return ClaudeResult(json.dumps({"segments": segs}), "end_turn")
        if "study library" in prompt:
            topics = [
                {
                    "name": "Getting Registered",
                    "sections": [{"name": "SAM.gov", "about": "the federal register"}],
                }
            ]
            return ClaudeResult(json.dumps({"notebook_title": "GovCon Basics", "topics": topics}), "end_turn")
        if "Place each numbered passage" in prompt:
            nums = _re.findall(r"^(\d+): ", prompt, _re.MULTILINE)
            pairs = [{"passage": int(n), "section": 0} for n in nums]
            return ClaudeResult(json.dumps({"assignments": pairs}), "end_turn")
        nums = [int(n) for n in _re.findall(r"^(\d+): \[", prompt, _re.MULTILINE)]
        return ClaudeResult(json.dumps({"intro": "How to register.", "order": nums}), "end_turn")

    def submit_batch(self, requests):
        from notebook_alchemy.errors import ClaudeError

        raise ClaudeError("batch", status=500, detail="HTTP 500: internal error")

    def usage(self):
        return []


def test_a_channel_becomes_topic_books_and_a_rerun_asks_the_ai_nothing(
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
    engine.ai = PipelineAI()  # type: ignore[assignment]
    report = engine.build("@chan")
    assert report.library.notebook_title == "GovCon Basics" and report.used_ai
    book = next(b for b in report.books if b.title == "Getting Registered")
    assert book.content.count("takes ten days") == 1  # said in all three videos, kept once
    assert all(f"vid0000000{i} adds its own fact" in book.content for i in range(3))  # what is new stays
    assert "Welcome back" not in book.content and not book.content.startswith("---")
    asked = len(engine.ai.prompts)
    engine.build("@chan")
    assert len(engine.ai.prompts) == asked  # labels, topics, placement and sections all cached
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
    standard = engine.estimate(
        [ChannelVideo("vid00000001", "t", "u", 600), ChannelVideo("vid00000002", "t", "u", 600)], batch=False
    )
    # Labels at half price in a batch; sorting (15% of labelling) always at the standard price.
    assert standard.cost_usd / est.cost_usd == pytest.approx((1 + 0.15) / (0.5 + 0.15))


def test_a_spend_limit_while_sorting_says_how_far_it_got_and_keeps_the_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_alchemy.channel import (
        ChannelScanner,
        ChannelScanResult,
        ChannelVideo,
        TranscriptManager,
        TranscriptResult,
    )
    from notebook_alchemy.errors import AlchemyError, ClaudeError

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

    class LimitWhenSorting(PipelineAI):
        def generate(self, prompt: str, **kw):
            if "study library" in prompt:
                raise ClaudeError(
                    "The Claude API account has no credit left, or its spend limit is reached.",
                    hint="Check Billing and Limits in the Claude Console; the same command continues "
                    "from here.",
                    stop_run=True,
                )
            return super().generate(prompt, **kw)

    engine = AlchemyEngine(_config(tmp_path))
    engine.ai = LimitWhenSorting()  # type: ignore[assignment]
    with pytest.raises(AlchemyError, match="1 of 1 videos are labelled and saved") as err:
        engine.build("@chan")
    assert "continues from here" in (err.value.hint or "")
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


# --- review findings: unchanged books whose upload was never confirmed ------------------------------


def _ids_by_title(fake: FakeNotebookLM) -> dict[str, str]:
    return {s["title"]: s["id"] for s in fake.sources[fake.notebook_id("Chan")]}


def test_a_book_whose_title_was_never_set_is_sent_again(tmp_path: Path) -> None:
    broken = FakeNotebookLM(reset_titles=True, fail={"rename": "Beta"})
    engine = _fake_engine(tmp_path, broken)
    with pytest.raises(SyncError):
        engine.publish("Chan", _books())
    assert sorted(_ids_by_title(broken)) == ["Alpha Topics", "Beta_Topics.md"]
    fixed = FakeNotebookLM(reset_titles=True)
    fixed.notebooks, fixed.sources = broken.notebooks, broken.sources
    engine.client = NotebookLMClient(engine.config, runner=fixed)
    result = engine.publish("Chan", _books())
    assert (result.uploaded, result.unchanged) == (1, 1)  # Beta's upload was never confirmed
    assert sorted(_ids_by_title(fixed)) == ["Alpha Topics", "Beta Topics"]


def test_an_unchanged_book_whose_title_drifted_is_renamed_not_resent(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    engine.publish("Chan", _books())
    beta = next(s for s in fake.sources[fake.notebook_id("Chan")] if s["title"] == "Beta Topics")
    beta["title"] = "Beta_Topics.md"  # NotebookLM reset it after the last read-back
    result = engine.publish("Chan", _books())
    assert (result.uploaded, result.unchanged) == (0, 2) and beta["title"] == "Beta Topics"


def test_an_old_copy_that_could_not_be_removed_is_removed_next_time(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    engine.publish("Chan", _books())
    old_beta = _ids_by_title(fake)["Beta Topics"]
    stubborn = FakeNotebookLM(fail={"delete": old_beta})
    stubborn.notebooks, stubborn.sources = fake.notebooks, fake.sources
    engine.client = NotebookLMClient(engine.config, runner=stubborn)
    alpha, beta = _books()
    with pytest.raises(SyncError, match="1 problems"):
        engine.publish("Chan", [alpha, *_edited([beta])])
    assert [s["title"] for s in fake.sources[fake.notebook_id("Chan")]].count("Beta Topics") == 2
    engine.client = NotebookLMClient(engine.config, runner=fake)
    result = engine.publish("Chan", [alpha, *_edited([beta])])
    assert result.uploaded == 0 and sorted(fake.titles("Chan")) == ["Alpha Topics", "Beta Topics"]
    assert old_beta not in _ids_by_title(fake).values()


def test_books_a_0_2_upload_recorded_under_its_own_file_name_are_replaced(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    nb = fake.seed("Chan", ["Old Domain: Basics", "_INDEX", "My own notes"])
    engine.publish("Chan", _books())  # a 0.3 upload: its record has ids, under the new name
    legacy = engine.sync_records_dir() / f"{nb.replace('-', '_')}.json"
    legacy.write_text(json.dumps({"titles": ["Old Domain: Basics", "_INDEX"]}), encoding="utf-8")
    assert legacy != engine._sync_state_path(nb)
    result = engine.publish("Chan", _books())
    assert fake.titles("Chan") == ["Alpha Topics", "Beta Topics", "My own notes"]  # yours stays
    assert result.pruned == 2 and not legacy.exists()


def test_the_transcript_route_stops_when_notebooklm_ends_the_sign_in(tmp_path: Path) -> None:
    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM(fail={"add-async": "watch"})  # every add: refused as signed out
    engine.client = NotebookLMClient(engine.config, runner=fake)

    real = fake._handle

    def signed_out(cmd):
        result = real(cmd)
        if result.returncode == 2 and "add-async" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Authentication expired or invalid.")
        return result

    fake._handle = signed_out  # type: ignore[method-assign]
    _really_signed_out(engine)
    videos = [(f"vid{i:08d}", f"Video {i}") for i in range(60)]  # three batches
    texts = engine.transcripts_via_notebooklm(videos)
    adds = [c for c in fake.calls if "add-async" in c]
    assert texts == {} and len(adds) == 1  # the first refusal stops the route
    assert engine.route_notices == [
        "The NotebookLM sign-in ended while reading videos; 60 videos wait for the next run."
    ]


def test_the_transcript_route_stops_when_the_sign_in_ends_while_reading(tmp_path: Path) -> None:
    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM(fail={"fulltext": "-"})  # every read: refused as signed out
    engine.client = NotebookLMClient(engine.config, runner=fake)
    real = fake._handle

    def signed_out(cmd):
        result = real(cmd)
        if result.returncode == 2 and "fulltext" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Authentication expired or invalid.")
        return result

    fake._handle = signed_out  # type: ignore[method-assign]
    _really_signed_out(engine)
    texts = engine.transcripts_via_notebooklm([(f"vid{i:08d}", f"Video {i}") for i in range(60)])
    assert texts == {} and len([c for c in fake.calls if "add-async" in c]) == 1  # no second batch
    assert "60 videos wait" in engine.route_notices[0]
    reads = [c for c in fake.calls if "fulltext" in c]
    assert len(reads) <= 3  # the reads already under way at most; the rest of the batch is skipped
    assert not [c for c in fake.calls if "delete" in c]  # nothing more is sent once signed out
    # The next run, signed in again, reads the videos left in the working notebook.
    fake._handle = real  # type: ignore[method-assign]
    fake.fail = {}
    again = engine.transcripts_via_notebooklm([(f"vid{i:08d}", f"Video {i}") for i in range(20)])
    assert len(again) == 20


def _really_signed_out(engine: AlchemyEngine) -> None:
    from notebook_alchemy.client import SessionStatus

    engine.client.check_session = lambda: SessionStatus(False, "signed out", signed_out=True)  # type: ignore[method-assign]


def test_one_video_whose_error_mentions_signing_in_does_not_stop_the_route(tmp_path: Path) -> None:
    from notebook_alchemy.client import SessionStatus

    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM()
    engine.client = NotebookLMClient(engine.config, runner=fake)
    real = fake._handle
    reads = iter(range(1000))

    def odd(cmd):
        if "fulltext" in cmd and next(reads) == 3:  # the fourth read: one video's own odd message
            return subprocess.CompletedProcess(cmd, 1, "", "This video requires login to view.")
        return real(cmd)

    fake._handle = odd  # type: ignore[method-assign]
    engine.client.check_session = lambda: SessionStatus(True, "Session is active.", 1)  # type: ignore[method-assign]
    texts = engine.transcripts_via_notebooklm([(f"vid{i:08d}", f"Video {i}") for i in range(40)])
    assert len(texts) == 39 and engine.route_notices == []  # one video waits; the rest are read


def test_a_sign_in_that_ended_before_the_route_starts_is_said_once(tmp_path: Path) -> None:
    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM()
    engine.client = NotebookLMClient(engine.config, runner=fake)

    def refused(cmd):
        return subprocess.CompletedProcess(cmd, 1, "", "Authentication expired or invalid.")

    fake._handle = refused  # type: ignore[method-assign]
    assert engine.transcripts_via_notebooklm([("vid00000001", "One"), ("vid00000002", "Two")]) == {}
    assert engine.route_notices == [
        "The NotebookLM sign-in ended while reading videos; 2 videos wait for the next run."
    ]


def test_one_batch_refused_with_a_sign_in_message_does_not_stop_the_route(tmp_path: Path) -> None:
    from notebook_alchemy.client import SessionStatus

    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM()
    engine.client = NotebookLMClient(engine.config, runner=fake)
    real = fake._handle
    adds = iter(range(1000))

    def odd(cmd):
        if "add-async" in cmd and next(adds) == 0:  # the first batch: an odd message, still signed in
            return subprocess.CompletedProcess(cmd, 1, "", "A video in this batch requires login.")
        return real(cmd)

    fake._handle = odd  # type: ignore[method-assign]
    engine.client.check_session = lambda: SessionStatus(True, "Session is active.", 1)  # type: ignore[method-assign]
    texts = engine.transcripts_via_notebooklm([(f"vid{i:08d}", f"Video {i}") for i in range(40)])
    assert len(texts) == 20 and engine.route_notices == []  # the second batch was still read


def test_a_delete_refused_as_signed_out_stops_the_route(tmp_path: Path) -> None:
    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM()
    engine.client = NotebookLMClient(engine.config, runner=fake)
    real = fake._handle

    deletes: list[list[str]] = []

    def refuse_deletes(cmd):
        if "delete" in cmd:
            deletes.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "Authentication expired or invalid.")
        return real(cmd)

    fake._handle = refuse_deletes  # type: ignore[method-assign]
    engine.transcripts_via_notebooklm([(f"vid{i:08d}", f"Video {i}") for i in range(60)])
    assert 1 <= len(deletes) <= 3 and len([c for c in fake.calls if "add-async" in c]) == 1
    assert "videos wait for the next run" in engine.route_notices[0]


def test_old_videos_are_not_deleted_one_by_one_after_a_sign_out(tmp_path: Path) -> None:
    _session(tmp_path)
    engine = AlchemyEngine(_config(tmp_path))
    fake = FakeNotebookLM()
    ws = fake.seed("Notebook Alchemy workspace", [])
    for i in range(5):  # videos an interrupted run left behind that are no longer wanted
        fake.sources[ws].append(
            {"id": f"yt-old-{i}", "title": f"Old {i}", "url": None, "type": "youtube", "status": "ready"}
        )
    engine.client = NotebookLMClient(engine.config, runner=fake)
    real = fake._handle
    refused: list[list[str]] = []

    def refuse_deletes(cmd):
        if "delete" in cmd:
            refused.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "Authentication expired or invalid.")
        return real(cmd)

    fake._handle = refuse_deletes  # type: ignore[method-assign]
    engine.transcripts_via_notebooklm([("vid00000001", "One")])
    assert len(refused) == 1  # the first refusal; the other four wait for the next run


def test_a_limit_while_labelling_stops_with_a_typed_error_and_uploads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_alchemy.channel import (
        ChannelScanner,
        ChannelScanResult,
        ChannelVideo,
        TranscriptManager,
        TranscriptResult,
    )
    from notebook_alchemy.errors import AlchemyError, ClaudeError

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

    class LimitOnBatch(PipelineAI):
        def submit_batch(self, requests):
            raise ClaudeError("The spend limit is reached.", hint="Check Billing.", stop_run=True)

    engine = AlchemyEngine(_config(tmp_path))
    engine.ai = LimitOnBatch()  # type: ignore[assignment]
    with pytest.raises(AlchemyError, match=r"spend limit is reached. Labels made so far are saved") as err:
        engine.build("@chan")
    assert not isinstance(err.value, ClaudeError) and err.value.hint == "Check Billing."


def test_words_a_model_wrote_down_are_not_used_as_the_speakers_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_alchemy.channel import (
        MODEL_WRITTEN,
        ChannelScanner,
        ChannelScanResult,
        ChannelVideo,
        TranscriptManager,
        TranscriptResult,
    )

    videos = [ChannelVideo("vid00000001", "One", "u", 600), ChannelVideo("vid00000002", "Two", "u", 600)]
    monkeypatch.setattr(
        ChannelScanner, "scan", lambda self, url, limit=None, **kw: ChannelScanResult("Chan", videos)
    )
    monkeypatch.setattr(
        TranscriptManager,
        "fetch_many",
        lambda self, items, progress=None: {
            "vid00000001": TranscriptResult("vid00000001", "captions said this", "youtube"),
            "vid00000002": TranscriptResult(
                "vid00000002", "a model wrote this", "cache", method=MODEL_WRITTEN
            ),
        },
    )
    report = AlchemyEngine(_config(tmp_path)).build("@chan", offline=True)
    assert report.read == {"vid00000001"}
    assert all("a model wrote this" not in b.content for b in report.books)


def test_nothing_is_sorted_while_videos_wait_for_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_alchemy.channel import (
        ChannelScanner,
        ChannelScanResult,
        ChannelVideo,
        TranscriptManager,
        TranscriptResult,
    )

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

    class StillRunning(PipelineAI):
        def submit_batch(self, requests):
            return "msgbatch_test"

        def batch_results(self, batch_id, keys):
            return "in_progress", None

    engine = AlchemyEngine(_config(tmp_path))
    engine.ai = StillRunning()  # type: ignore[assignment]
    import notebook_alchemy.knowledge as knowledge

    real_init = knowledge.Labeler.__init__

    def quick(self, *a, **kw):
        real_init(self, *a, **{**kw, "batch_wait": 0, "sleep": lambda s: None})

    monkeypatch.setattr(knowledge.Labeler, "__init__", quick)
    report = engine.build("@chan")
    assert report.waiting == ["vid00000001"] and report.books == []
    assert engine.ai.prompts == []  # no topic map, placement or order was asked for
    assert RequestCache(engine.library_cache_path("Chan")).get("map:Chan") is None


def test_unsorted_books_do_not_quietly_replace_ai_sorted_ones(tmp_path: Path) -> None:
    from notebook_alchemy.errors import AlchemyError

    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    first = engine.publish("Chan", _books(), channel_key="Chan", sorted_by_ai=True, videos_read=10)
    sources = len(fake.sources[first.notebook_id])
    with pytest.raises(AlchemyError, match="have no AI sorting"):
        engine.publish("Chan", _books(), channel_key="Chan", sorted_by_ai=False, videos_read=10)
    assert len(fake.sources[first.notebook_id]) == sources  # nothing uploaded or removed
    engine.publish(
        "Chan", _books(), channel_key="Chan", sorted_by_ai=False, videos_read=10, allow_unsorted=True
    )


def test_a_much_smaller_build_does_not_quietly_shrink_the_notebook(tmp_path: Path) -> None:
    from notebook_alchemy.errors import AlchemyError

    engine = _fake_engine(tmp_path, FakeNotebookLM())
    engine.publish("Chan", _books(), channel_key="Chan", videos_read=100)
    with pytest.raises(AlchemyError, match="from 10 videos; the notebook's come from 100"):
        engine.publish("Chan", _books(), channel_key="Chan", videos_read=10)
    engine.publish("Chan", _books(), channel_key="Chan", videos_read=60)  # half or more is fine
    engine.publish("Chan", _books(), channel_key="Chan", videos_read=10, allow_fewer=True)


def test_a_notebook_recorded_under_the_typed_link_is_still_found(tmp_path: Path) -> None:
    fake = FakeNotebookLM()
    engine = _fake_engine(tmp_path, fake)
    first = engine.publish("Chan", _books(), channel_key="@chan")
    # A new title, so only the record (not a notebook with the same title) can lead back to it.
    again = engine.publish("A Better Title", _books(), channel_key="Chan", old_channel_key="@chan")
    assert again.notebook_id == first.notebook_id and len(fake.notebooks) == 1
    assert engine.notebook_for("Chan") == first.notebook_id


def test_an_upload_record_that_cannot_be_saved_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _fake_engine(tmp_path, FakeNotebookLM())
    monkeypatch.setattr(engine, "_write_json", lambda path, data, what: False)
    assert engine.publish("Chan", _books()).record_saved is False


def test_cached_words_a_model_wrote_down_are_read_again(tmp_path: Path) -> None:
    from notebook_alchemy.channel import MODEL_WRITTEN

    engine = AlchemyEngine(_config(tmp_path))
    engine.transcripts._write_cache("vid00000001", "t", "a model wrote this", method=MODEL_WRITTEN)
    engine.transcripts._write_cache("vid00000002", "t", "captions said this", method="youtube")
    assert engine.transcripts._read_cache("vid00000001") is None  # not used: the video is read again
    assert engine.transcripts._read_cache("vid00000002").text == "captions said this"
