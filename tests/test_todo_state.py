"""Tests for aura.todo_state — todo_task_status recognition."""

from __future__ import annotations

from aura.todo_state import normalize_todo_tasks, todo_signature, todo_task_status


def _s(raw_status: str) -> dict:
    return {"status": raw_status}


def test_done_variants():
    for v in ("done", "completed", "complete"):
        assert todo_task_status(_s(v)) == "done"


def test_active_variants():
    for v in ("active", "in_progress", "doing", "current"):
        assert todo_task_status(_s(v)) == "active"


def test_failed_variants():
    for v in ("failed", "fail", "error"):
        assert todo_task_status(_s(v)) == "failed"


def test_skipped_variants():
    for v in ("skipped", "skip", "cancelled", "canceled"):
        assert todo_task_status(_s(v)) == "skipped"


def test_pending_fallback():
    for v in ("pending", "unknown", "", "whatever"):
        assert todo_task_status(_s(v)) == "pending"


def test_todo_task_status_failed():
    """Failed status mapping including case insensitivity."""
    assert todo_task_status(_s("failed")) == "failed"
    assert todo_task_status(_s("fail")) == "failed"
    assert todo_task_status(_s("error")) == "failed"
    assert todo_task_status(_s("FaiLed")) == "failed"


def test_todo_task_status_skipped():
    """Skipped status mapping."""
    assert todo_task_status(_s("skipped")) == "skipped"
    assert todo_task_status(_s("skip")) == "skipped"
    assert todo_task_status(_s("cancelled")) == "skipped"
    assert todo_task_status(_s("canceled")) == "skipped"


def test_todo_task_status_preserves_existing():
    """Existing statuses are preserved through normalization."""
    assert todo_task_status(_s("done")) == "done"
    assert todo_task_status(_s("active")) == "active"
    assert todo_task_status(_s("pending")) == "pending"
    assert todo_task_status(_s("")) == "pending"
    assert todo_task_status(_s("unknown")) == "pending"


def test_todo_signature_preserves_failed_skipped():
    """normalize_todo_tasks and todo_signature preserve failed and skipped statuses."""
    tasks = [
        {"description": "task a", "status": "failed"},
        {"description": "task b", "status": "skipped"},
    ]
    normalized = normalize_todo_tasks(tasks)
    assert normalized[0]["status"] == "failed"
    assert normalized[1]["status"] == "skipped"
    sig = todo_signature(tasks)
    assert sig == (("task a", "failed"), ("task b", "skipped"))


def test_normalize_todo_tasks_all_statuses():
    """All five canonical statuses survive a round-trip through normalize."""
    tasks = [
        {"description": "p", "status": "pending"},
        {"description": "a", "status": "active"},
        {"description": "d", "status": "done"},
        {"description": "f", "status": "failed"},
        {"description": "s", "status": "skipped"},
    ]
    normalized = normalize_todo_tasks(tasks)
    assert [t["status"] for t in normalized] == [
        "pending", "active", "done", "failed", "skipped",
    ]
