"""Durable receipts for long MCP runs, with one active run per server process."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

logger = logging.getLogger(__name__)


def receipts_dir() -> Path:
    return Path.home() / ".notebook-alchemy" / "runs"


class RunRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._active: str | None = None
        self._file_lock = FileLock(root / "active.lock", thread_local=False)

    def _active_path(self) -> Path:
        return self.root / "active.json"

    def _path(self, run_id: str) -> Path:
        if len(run_id) != 32 or any(ch not in "0123456789abcdef" for ch in run_id):
            raise ValueError("Invalid run ID")
        return self.root / f"{run_id}.json"

    def _write(self, record: dict[str, Any]) -> None:
        path = self._path(record["run_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, path)

    def _write_active(self, run_id: str) -> None:
        temporary = self.root / "active.tmp"
        temporary.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        os.replace(temporary, self._active_path())

    def start(
        self, task: Callable[[], dict[str, Any]], error_text: Callable[[Exception], str]
    ) -> dict[str, Any]:
        with self._lock:
            if self._active is not None:
                return {"status": "already_running", "run_id": self._active}
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                self._file_lock.acquire(timeout=0)
            except Timeout:
                try:
                    active = json.loads(self._active_path().read_text(encoding="utf-8"))
                    active_id = active.get("run_id")
                except (OSError, ValueError):
                    active_id = None
                return {"status": "already_running", "run_id": active_id}
            run_id = uuid.uuid4().hex
            record: dict[str, Any] = {
                "run_id": run_id,
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
            }
            try:
                self._write(record)
                self._write_active(run_id)
            except OSError:
                self._file_lock.release()
                raise
            self._active = run_id

        def execute() -> None:
            try:
                result = task()
                final = {**record, "status": "completed", "result": result}
            except Exception as exc:  # noqa: BLE001 - the receipt must record a failed background task
                try:
                    safe_error = error_text(exc)
                except Exception:  # noqa: BLE001 - error formatting must not strand the run lock
                    safe_error = f"Run failed ({type(exc).__name__}). Check local logs before retrying."
                final = {**record, "status": "failed", "error": safe_error}
            final["finished_at"] = datetime.now(UTC).isoformat()
            with self._lock:
                try:
                    self._write(final)
                except OSError as exc:
                    logger.error("Could not save the final MCP run receipt: %s", type(exc).__name__)
                finally:
                    self._active = None
                    try:
                        self._active_path().unlink(missing_ok=True)
                    finally:
                        self._file_lock.release()

        try:
            threading.Thread(target=execute, name=f"alchemy-run-{run_id[:8]}", daemon=True).start()
        except RuntimeError:
            with self._lock:
                self._active = None
                try:
                    self._active_path().unlink(missing_ok=True)
                finally:
                    self._file_lock.release()
            raise
        return record

    def status(self, run_id: str) -> dict[str, Any]:
        path = self._path(run_id)
        with self._lock:
            if not path.is_file():
                return {"run_id": run_id, "status": "not_found"}
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {
                    "run_id": run_id,
                    "status": "receipt_unreadable",
                    "next_action": "Check access to the local run receipt before retrying.",
                }
            if record.get("status") == "running" and self._active != run_id:
                probe = FileLock(self.root / "active.lock", thread_local=False)
                try:
                    probe.acquire(timeout=0)
                except Timeout:
                    try:
                        active = json.loads(self._active_path().read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        active = {}
                    if active.get("run_id") == run_id:
                        return record
                else:
                    probe.release()
                return {
                    **record,
                    "status": "interrupted",
                    "next_action": "Run alchemy_run again; cached work is reused.",
                }
            return record


def is_completed_run_receipt(path: Path) -> bool:
    """Only completed receipts belong to record cleanup; an active run keeps its receipt."""
    if len(path.stem) != 32 or any(ch not in "0123456789abcdef" for ch in path.stem):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("run_id") == path.stem and data.get("status") in ("completed", "failed")
