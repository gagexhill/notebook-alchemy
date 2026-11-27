"""Long MCP runs return a receipt without starting duplicate work."""

from __future__ import annotations

import threading

import pytest

from notebook_alchemy.run_registry import RunRegistry


def test_run_receipt_reports_completion_and_prevents_duplicate(tmp_path) -> None:
    gate = threading.Event()
    count = 0

    def task():
        nonlocal count
        count += 1
        gate.wait(timeout=2)
        return {"status": "saved", "ai_usage": []}

    registry = RunRegistry(tmp_path)
    started = registry.start(task, lambda exc: type(exc).__name__)
    assert started["status"] == "running"
    assert registry.start(task, lambda exc: type(exc).__name__) == {
        "status": "already_running",
        "run_id": started["run_id"],
    }
    assert RunRegistry(tmp_path).start(task, lambda exc: type(exc).__name__) == {
        "status": "already_running",
        "run_id": started["run_id"],
    }
    assert RunRegistry(tmp_path).status(started["run_id"])["status"] == "running"
    gate.set()
    for _ in range(100):
        receipt = registry.status(started["run_id"])
        if receipt["status"] != "running":
            break
        threading.Event().wait(timeout=0.01)
    assert receipt["status"] == "completed"
    assert receipt["result"]["status"] == "saved"
    assert count == 1
    assert RunRegistry(tmp_path).status(started["run_id"])["status"] == "completed"


def test_run_receipt_reports_failed_task(tmp_path) -> None:
    finished = threading.Event()

    def task():
        finished.set()
        raise ValueError("synthetic failure")

    registry = RunRegistry(tmp_path)
    started = registry.start(task, lambda exc: type(exc).__name__)
    assert finished.wait(timeout=2)
    for _ in range(100):
        receipt = registry.status(started["run_id"])
        if receipt["status"] != "running":
            break
        threading.Event().wait(timeout=0.01)
    assert receipt["status"] == "failed"
    assert receipt["error"] == "ValueError"


def test_run_receipt_releases_lock_if_error_formatting_fails(tmp_path) -> None:
    def task():
        raise ValueError("synthetic failure")

    def bad_formatter(exc):
        raise RuntimeError("synthetic formatter failure")

    registry = RunRegistry(tmp_path)
    started = registry.start(task, bad_formatter)
    for _ in range(100):
        receipt = registry.status(started["run_id"])
        if receipt["status"] != "running":
            break
        threading.Event().wait(timeout=0.01)
    assert receipt["status"] == "failed"
    assert "ValueError" in receipt["error"]
    assert RunRegistry(tmp_path).start(lambda: {}, lambda exc: "error")["status"] == "running"


def test_run_receipt_marks_interrupted_and_rejects_invalid_ids(tmp_path) -> None:
    registry = RunRegistry(tmp_path)
    run_id = "a" * 32
    registry._write({"run_id": run_id, "status": "running"})
    assert registry.status(run_id)["status"] == "interrupted"
    with pytest.raises(ValueError, match="Invalid run ID"):
        registry.status("../outside")
