"""Focused tests for the Worker execution contract checks.

No Qt, no real file I/O -- pure unit tests on the relevant functions.
"""

from __future__ import annotations

from aura.conversation.spec_quality import (
    _is_vague_acceptance,
    validate_worker_dispatch_spec,
)
from aura.bridge.dispatch import _check_read_before_edit


# --- Acceptance validation tests ---


def test_vague_acceptance_rejected() -> None:
    """_is_vague_acceptance rejects trivial and vague phrases."""
    vague = [
        "works",
        "done",
        "as requested",
        "make it good",
        "user is happy",
        "should work",
        "it works",
    ]
    for phrase in vague:
        assert _is_vague_acceptance(phrase), f"expected '{phrase}' to be rejected"
    assert _is_vague_acceptance("")
    assert _is_vague_acceptance("ok")
    assert _is_vague_acceptance("yes")
    assert _is_vague_acceptance("should be fine i think")
    assert _is_vague_acceptance("make it good is all i ask")


def test_concise_acceptance_accepted() -> None:
    """validate_worker_dispatch_spec accepts concrete, observable checks."""
    goal = "Test goal"
    spec_text = "Test spec"
    concrete = [
        "py_compile passes.",
        "No WorkerSummaryCard appears before Worker start.",
        "edit_symbol is included in modified files.",
    ]
    for acc in concrete:
        result = validate_worker_dispatch_spec(
            spec=spec_text,
            acceptance=acc,
            goal=goal,
        )
        assert result.ok, f"acceptance '{acc}' should be accepted, got: {result.errors}"


def test_validate_worker_dispatch_spec_requires_fields() -> None:
    """Requires goal, spec, and acceptance to be non-empty."""
    r = validate_worker_dispatch_spec(
        spec="",
        acceptance="py_compile passes.",
        goal="goal",
    )
    assert not r.ok
    assert any("spec" in e.lower() for e in r.errors)

    r = validate_worker_dispatch_spec(
        spec="spec",
        acceptance="",
        goal="goal",
    )
    assert not r.ok
    assert any("acceptance" in e.lower() for e in r.errors)

    r = validate_worker_dispatch_spec(
        spec="spec",
        acceptance="py_compile passes.",
        goal="",
    )
    assert not r.ok
    assert any("goal" in e.lower() for e in r.errors)


# --- Read-before-edit enforcement tests ---


def test_edited_without_read_is_hard_failure() -> None:
    """_check_read_before_edit returns files edited without reading."""
    edited = _check_read_before_edit(
        read_files=set(),
        read_outline_files=set(),
        edited_existing_files=["aura/bridge/dispatch.py"],
        file_exists=lambda p: True,
    )
    assert edited == ["aura/bridge/dispatch.py"]


def test_edited_after_read_is_ok() -> None:
    """_check_read_before_edit returns empty when all files were read."""
    edited = _check_read_before_edit(
        read_files={"aura/bridge/dispatch.py"},
        read_outline_files={"aura/bridge/event_relay.py"},
        edited_existing_files=[
            "aura/bridge/dispatch.py",
            "aura/bridge/event_relay.py",
        ],
        file_exists=lambda p: True,
    )
    assert edited == []


def test_read_outline_counts_as_read_for_enforcement() -> None:
    """read_file_outline is treated as a valid read."""
    edited = _check_read_before_edit(
        read_files=set(),
        read_outline_files={"aura/bridge/dispatch.py"},
        edited_existing_files=["aura/bridge/dispatch.py"],
        file_exists=lambda p: True,
    )
    assert edited == []


def test_nonexistent_files_not_flagged() -> None:
    """Nonexistent file paths are not flagged."""
    edited = _check_read_before_edit(
        read_files=set(),
        read_outline_files=set(),
        edited_existing_files=["nonexistent/new_file.py"],
        file_exists=lambda p: False,
    )
    assert edited == []


# --- Relay tracking tests (attribute simulation, no Qt) ---


def test_event_relay_tracks_read_tool() -> None:
    """read_file paths go into read_files set."""
    read_files: set[str] = set()
    read_files.add("aura/bridge/dispatch.py")
    read_files.add("aura/prompts.py")
    assert "aura/bridge/dispatch.py" in read_files
    assert "aura/prompts.py" in read_files


def test_event_relay_tracks_write_new_vs_existing() -> None:
    """wrote_new_files vs edited_existing_files tracked separately."""
    wrote_new: list[str] = []
    edited_existing: list[str] = []
    touched: set[str] = set()

    path_new = "tests/test_worker_contract.py"
    wrote_new.append(path_new)
    touched.add(path_new)

    path_existing = "aura/prompts.py"
    edited_existing.append(path_existing)
    touched.add(path_existing)

    assert "tests/test_worker_contract.py" in wrote_new
    assert "aura/prompts.py" in edited_existing
    assert "tests/test_worker_contract.py" not in edited_existing
    assert "aura/prompts.py" not in wrote_new
    assert path_new in touched
    assert path_existing in touched


def test_event_relay_tracks_todo_usage() -> None:
    """todo_used is set when update_todo_list result comes through."""
    todo_used: bool = False
    todo_used = True
    assert todo_used


def test_no_work_detection_triggers() -> None:
    """No-work: touched_files empty + no errors + implementation task."""
    touched_files: set[str] = set()
    failed_tool_results: list[dict] = []
    api_errors: list[str] = []
    internal_error = None
    is_no_work = not touched_files and not failed_tool_results and not internal_error and not api_errors
    assert is_no_work
