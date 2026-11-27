"""Transport-neutral service layer shared by the CLI and the MCP server.

All functions here avoid interactive terminal prompts (`click.prompt`, `click.confirm`) and never
print to stdout, so both Click commands and MCP JSON-RPC stdio/HTTP handlers share one tested core.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .channel import DEFAULT_TABS
from .claude import price_for as claude_price_for
from .client import AccountIdentity, consent_file, record_upload_consent, upload_confirmed
from .config import AlchemyConfig, resolve_env_paths
from .engine import AlchemyEngine, ProgressFn, _home_relative
from .errors import AlchemyError, SessionError
from .keepalive import KeepAlive
from .run_registry import is_completed_run_receipt, receipts_dir
from .watch import price_for as gemini_price_for
from .writer import OVERVIEW_TITLE

NOTEBOOKLM_URL = "https://notebooklm.google.com"


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_sync_record(path: Path) -> bool:
    """A record this tool wrote after an upload: a JSON object whose `titles` is a list of strings."""
    data = _read_json(path)
    titles = data.get("titles") if isinstance(data, dict) else None
    return isinstance(titles, list) and all(isinstance(t, str) for t in titles)


def is_cache_record(path: Path) -> bool:
    """True for a caption cache file this tool wrote (JSON object with a video_id)."""
    data = _read_json(path)
    return isinstance(data, dict) and "video_id" in data


def is_library_cache(path: Path) -> bool:
    """The sorting answers this tool keeps per channel: keys are hashes or map:/placed:/sub: names."""
    data = _read_json(path)
    return isinstance(data, dict) and all(
        k == "parts" or k.startswith(("map:", "placed:", "sub:")) or re.fullmatch(r"[0-9a-f]{32}", k)
        for k in data
    )


def is_batch_state(path: Path) -> bool:
    """A labelling batch's state: only the keys this tool writes there."""
    data = _read_json(path)
    return isinstance(data, dict) and set(data) <= {"job", "keys", "model", "answers"}


def is_string_map(path: Path) -> bool:
    data = _read_json(path)
    return isinstance(data, dict) and all(isinstance(v, str) for v in data.values())


def is_label_record(path: Path) -> bool:
    data = _read_json(path)
    return isinstance(data, dict) and "segments" in data and "key" in data


def get_status(config: AlchemyConfig, *, check_live: bool = False) -> dict[str, Any]:
    """Return structured health and configuration status (no secrets)."""
    engine = AlchemyEngine(config)
    try:
        facts = engine.status()
        identity = engine.client.upload_account()
        keep_state = KeepAlive(engine.client.profile, engine.home).state()
        notebooks_data = _read_json(engine.notebooks_path())
        mapped = notebooks_data if isinstance(notebooks_data, dict) else {}
        batches_dir = engine.home / "batches"
        active_batches = [
            f.stem
            for f in (sorted(batches_dir.glob("*.json")) if batches_dir.is_dir() else [])
            if _read_json(f)
        ]

        live: dict[str, Any] | None = None
        if check_live:
            session = engine.client.check_session()
            limits = engine.client.plan_limits() if session.ok else None
            live = {
                "ok": session.ok,
                "signed_out": session.signed_out,
                "detail": session.detail,
                "notebooks": session.notebooks,
                "plan": limits.plan if limits else None,
                "source_limit": limits.source_limit if limits else None,
                "notebook_limit": limits.notebook_limit if limits else None,
            }

        return {
            "environment": facts["environment"],
            "notebooklm_cli": facts["notebooklm_cli"],
            "notebooklm_profile": facts["notebooklm_profile"],
            "session_file": facts["session_file"],
            "authenticated": engine.client.is_authenticated(),
            "upload_account": {
                "email": identity.email,
                "profile": identity.profile,
                "detail": identity.detail,
                "upload_confirmed": upload_confirmed(identity),
            },
            "keepalive": {
                "on": keep_state.on,
                "where": keep_state.where,
                "stale": keep_state.stale,
                "last_at": keep_state.last_at,
                "last_ok": keep_state.last_ok,
                "last_detail": keep_state.last_detail,
            },
            "claude": {"configured": config.has_api_key, "model": config.model},
            "gemini_watch": {"configured": config.has_gemini_key, "model": config.gemini_model},
            "env_files": [_home_relative(p) for p in resolve_env_paths()],
            "cache_dir": facts["cache_dir"],
            "mapped_channels": {str(k): str(v) for k, v in mapped.items() if k != "__workspace__"},
            "active_batches": active_batches,
            "live": live,
        }
    finally:
        engine.client.close()


def preview_channel(
    config: AlchemyConfig,
    source: str,
    *,
    limit: int | None = None,
    tabs: tuple[str, ...] = DEFAULT_TABS,
    mock: bool = False,
    now: bool = False,
    watch_limit: int = 0,
) -> dict[str, Any]:
    """Scan a channel/playlist/video and estimate cost without modifying NotebookLM or spending."""
    engine = AlchemyEngine(config)
    try:
        channel, videos = engine.scan(source, limit=limit, tabs=tabs, mock=mock)
        est = engine.estimate(videos, mock=mock, batch=not now, watch_limit=watch_limit)
        claude_note = claude_price_for(est.model)[2] if config.has_api_key and not mock else None
        gemini_note = gemini_price_for(est.gemini_model)[2] if watch_limit > 0 and not mock else None
        return {
            "channel": channel,
            "source": source,
            "is_mock": mock,
            "videos": est.videos,
            "tab_counts": engine.last_scan.tab_counts if engine.last_scan else {},
            "shorts_excluded": engine.last_scan.excluded_shorts if engine.last_scan else None,
            "incomplete_tabs": engine.last_scan.incomplete_tabs if engine.last_scan else [],
            "videos_known": est.videos_known,
            "videos_without_words": est.videos - est.videos_known,
            "words_known": est.words_known,
            "words_estimated": est.words_estimated,
            "labelled": est.labelled,
            "unlabelled": max(0, est.videos - est.labelled),
            "claude_configured": config.has_api_key,
            "claude_model": est.model,
            "batch_mode": not now,
            "estimated_claude_usd": round(est.cost_usd, 4) if config.has_api_key and not mock else 0.0,
            "watch_limit": watch_limit,
            "watch_videos_estimated": est.watch_videos,
            "gemini_model": est.gemini_model or config.gemini_model,
            "estimated_watch_usd": round(est.watch_cost_usd, 4),
            "estimated_total_usd": round(
                (est.cost_usd if config.has_api_key and not mock else 0.0) + est.watch_cost_usd, 4
            ),
            "notes": [n for n in (claude_note, gemini_note) if n],
        }
    finally:
        engine.client.close()


def _verify_upload_account(engine: AlchemyEngine, *, confirm_upload: bool) -> AccountIdentity:
    session = engine.client.check_session()
    if not session.ok:
        raise SessionError(
            session.detail,
            hint="Sign in with `notebook-alchemy login` in a terminal, or pass save_dir to save locally.",
        )
    identity = engine.client.upload_account()
    if not upload_confirmed(identity):
        if not confirm_upload:
            raise SessionError(
                f"Upload confirmation required for {identity.display} "
                f"(profile '{identity.profile}'). Nothing was uploaded.",
                hint="Confirm this Google account with the user and pass confirm_upload=True, "
                "or pass save_dir to write Markdown files locally instead.",
            )
        if identity.email:
            record_upload_consent(identity)
    return identity


def run_pipeline_service(
    config: AlchemyConfig,
    source: str,
    *,
    notebook_name: str | None = None,
    save_dir: Path | None = None,
    limit: int | None = None,
    tabs: tuple[str, ...] = DEFAULT_TABS,
    offline: bool = False,
    mock: bool = False,
    now: bool = False,
    resort: bool = False,
    watch_limit: int = 0,
    watch_budget_usd: float = 0.0,
    allow_fewer: bool = False,
    confirm_upload: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Run the full build (and upload or local save) non-interactively."""
    if mock and save_dir is None:
        raise AlchemyError(
            "Sample data is never uploaded to NotebookLM.",
            hint="Provide save_dir to write the sample topic books locally.",
        )
    engine = AlchemyEngine(config)
    try:
        upload = save_dir is None and not mock
        confirmed: AccountIdentity | None = None
        if upload:
            confirmed = _verify_upload_account(engine, confirm_upload=confirm_upload)

        report = engine.build(
            source,
            limit=limit,
            tabs=tabs,
            mock=mock,
            offline=offline,
            resort=resort,
            use_batch=not now,
            watch_limit=watch_limit,
            watch_budget_usd=watch_budget_usd,
            progress=progress,
        )
        ai_usage = [
            {"model": m, "input_tokens": inp, "output_tokens": out, "cost_usd": round(cost, 6)}
            for m, inp, out, cost in (engine.ai.usage() if engine.ai else [])
        ]
        watch_usage = None
        if engine.watcher and (wu := engine.watcher.usage()):
            watch_usage = {
                "model": wu[0],
                "input_tokens": wu[1],
                "output_tokens": wu[2],
                "cost_usd": round(wu[3], 6),
            }

        if report.waiting:
            return {
                "status": "waiting_for_batch",
                "channel": report.channel,
                "videos_total": len(report.videos),
                "tab_counts": engine.last_scan.tab_counts if engine.last_scan else {},
                "shorts_excluded": engine.last_scan.excluded_shorts if engine.last_scan else None,
                "videos_read": len(report.read),
                "videos_without_words": len(report.unread),
                "unread_videos": report.unread,
                "labelled_now": report.labelled,
                "waiting_videos": len(report.waiting),
                "notices": report.notices,
                "ai_usage": ai_usage,
                "watch_usage": watch_usage,
            }

        topic_books = [b for b in report.books if b.title != OVERVIEW_TITLE]
        base: dict[str, Any] = {
            "channel": report.channel,
            "notebook_title": notebook_name or report.library.notebook_title,
            "videos_total": len(report.videos),
            "tab_counts": engine.last_scan.tab_counts if engine.last_scan else {},
            "shorts_excluded": engine.last_scan.excluded_shorts if engine.last_scan else None,
            "videos_read": len(report.read),
            "videos_without_words": len(report.unread),
            "unread_videos": report.unread,
            "watched_videos": sorted(report.watched),
            "used_ai": report.used_ai,
            "labelled_now": report.labelled,
            "books_count": len(topic_books),
            "books": [{"title": b.title, "words": b.words} for b in report.books],
            "total_words": sum(b.words for b in report.books),
            "removed_duplicates": report.library.removed_duplicates,
            "removed_outdated": len(report.library.removed_outdated),
            "by_source": report.by_source,
            "notices": report.notices,
            "ai_usage": ai_usage,
            "watch_usage": watch_usage,
        }

        if save_dir is not None:
            paths = engine.save_books(report.books, save_dir)
            return {
                "status": "saved",
                **base,
                "saved_dir": str(save_dir),
                "saved_files": [str(p) for p in paths],
            }

        assert confirmed is not None  # noqa: S101 - upload implies confirmed
        now_identity = engine.client.upload_account()
        if (now_identity.email or "").casefold() != (confirmed.email or "").casefold():
            raise SessionError(
                f"The upload account changed during the build: was {confirmed.display}, now "
                f"{now_identity.display}. Nothing was uploaded.",
                hint="Check the account with `notebook-alchemy status`, then run again.",
            )

        title = notebook_name or report.library.notebook_title
        synced = engine.publish(
            title,
            report.books,
            channel_key=None if notebook_name or mock else report.channel,
            old_channel_key=None if notebook_name else source,
            sorted_by_ai=report.library.sorted_by_ai,
            videos_read=len(report.read),
            allow_unsorted=offline,
            allow_fewer=allow_fewer,
            progress=progress,
        )
        return {
            "status": "uploaded",
            **base,
            "notebook_id": synced.notebook_id,
            "notebook_url": NOTEBOOKLM_URL,
            "created": synced.created,
            "uploaded": synced.uploaded,
            "replaced": synced.replaced,
            "pruned": synced.pruned,
            "unchanged": synced.unchanged,
            "source_limit": synced.source_limit,
            "freed_first": synced.freed_first,
            "record_saved": synced.record_saved,
        }
    finally:
        engine.client.close()


def list_notebooks_service(
    config: AlchemyConfig,
    *,
    notebook_query: str | None = None,
    include_sources: bool = False,
) -> dict[str, Any]:
    """List mapped channels, sync records, and optionally live NotebookLM notebooks/sources."""
    engine = AlchemyEngine(config)
    try:
        raw_map = _read_json(engine.notebooks_path())
        mapped = {
            str(k): str(v)
            for k, v in (raw_map.items() if isinstance(raw_map, dict) else [])
            if k != "__workspace__"
        }
        sync_dir = engine.sync_records_dir()
        records: dict[str, Any] = {}
        if sync_dir.is_dir():
            for path in sorted(sync_dir.glob("*.json")):
                if is_sync_record(path):
                    data = _read_json(path)
                    if isinstance(data, dict):
                        records[path.stem] = {
                            "titles": data.get("titles", []),
                            "orphans": data.get("orphans", []),
                            "sorted_by_ai": data.get("sorted_by_ai"),
                            "videos_read": data.get("videos_read", 0),
                        }

        live_notebooks: list[dict[str, Any]] | None = None
        selected_sources: list[dict[str, Any]] | None = None
        selected_notebook: dict[str, Any] | None = None
        if include_sources or notebook_query:
            session = engine.client.check_session()
            if not session.ok:
                raise SessionError(session.detail, hint="Sign in with `notebook-alchemy login`.")
            live_notebooks = engine.client.list_notebooks()
            if notebook_query:
                found = engine.client.find_notebook(notebook_query)
                if found and found.get("id"):
                    selected_notebook = found
                    selected_sources = engine.client.list_sources(str(found["id"]))
            elif include_sources:
                selected_sources_by_nb = []
                for nb in live_notebooks:
                    nb_id = str(nb["id"])
                    if nb_id in mapped.values() or nb_id in records:
                        srcs = engine.client.list_sources(nb_id)
                        selected_sources_by_nb.append(
                            {
                                "id": nb_id,
                                "title": nb.get("title", ""),
                                "sources": srcs,
                            }
                        )
                live_notebooks = selected_sources_by_nb

        return {
            "profile": engine.client.profile,
            "mapped_channels": mapped,
            "sync_records": records,
            "live_notebooks": live_notebooks,
            "selected_notebook": selected_notebook,
            "selected_sources": selected_sources,
        }
    finally:
        engine.client.close()


def clean_service(
    config: AlchemyConfig,
    *,
    cache: bool = False,
    labels: bool = False,
    records: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Remove Notebook Alchemy working files safely, requiring confirm=True for costly deletes."""
    engine = AlchemyEngine(config)
    do_cache = cache or not (labels or records)
    targets: list[tuple[str, Path]] = []
    skipped: list[str] = []
    custom_cache = config.cache_dir != AlchemyConfig().cache_dir

    if do_cache and config.cache_dir.is_dir():
        for f in sorted(config.cache_dir.glob("*.json")):
            (targets.append(("cache", f)) if is_cache_record(f) else skipped.append(f.name))
    if labels:
        for f in sorted(engine.knowledge_dir().glob("*.json")) if engine.knowledge_dir().is_dir() else []:
            (targets.append(("labels", f)) if is_label_record(f) else skipped.append(str(f)))
        library = engine.home / "library"
        for f in sorted(library.glob("*.json")) if library.is_dir() else []:
            (targets.append(("labels", f)) if is_library_cache(f) else skipped.append(str(f)))
        batches = engine.home / "batches"
        for f in [*(sorted(batches.glob("*.json")) if batches.is_dir() else []), engine.home / "batch.json"]:
            if f.is_file():
                (targets.append(("labels", f)) if is_batch_state(f) else skipped.append(str(f)))
        watched = engine.home / "watched.json"
        if watched.is_file():
            (targets.append(("labels", watched)) if is_string_map(watched) else skipped.append(str(watched)))
    if records:
        runs = receipts_dir()
        for f in sorted(runs.glob("*.json")) if runs.is_dir() else []:
            (targets.append(("record", f)) if is_completed_run_receipt(f) else skipped.append(str(f)))
        records_dir = engine.sync_records_dir()
        for f in sorted(records_dir.glob("*.json")) if records_dir.is_dir() else []:
            (targets.append(("record", f)) if is_sync_record(f) else skipped.append(str(f)))
        if consent_file().is_file():
            targets.append(("record", consent_file()))
        for f in (engine.notebooks_path(), engine.home / "keepalive.json"):
            if f.is_file():
                shaped = is_string_map(f) if f == engine.notebooks_path() else isinstance(_read_json(f), dict)
                (targets.append(("record", f)) if shaped else skipped.append(str(f)))

    if not targets:
        return {"removed": 0, "freed_bytes": 0, "skipped": skipped}

    kinds = {kind for kind, _p in targets}
    costly = bool(kinds & {"labels", "record"} or ("cache" in kinds and custom_cache))
    if costly and not confirm:
        raise AlchemyError(
            "Deleting labels, records, or a custom caption folder requires confirmation.",
            hint="Pass confirm=True (or --yes) to confirm deletion.",
        )

    freed = 0
    removed = 0
    failed: list[str] = []
    for _kind, path in targets:
        try:
            size = (
                sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
                if path.is_dir()
                else path.stat().st_size
            )
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            freed += size
            removed += 1
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    if failed:
        raise AlchemyError(
            f"{len(failed)} item(s) could not be deleted: " + "; ".join(failed[:3]),
            hint="Close programs using those files and try again.",
        )
    return {"removed": removed, "freed_bytes": freed, "skipped": skipped}
