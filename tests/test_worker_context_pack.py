"""Tests for the Worker Context Pack package."""

from __future__ import annotations

from pathlib import Path

from aura.conversation.context_pack import build_worker_context_pack


def test_basic_pack(tmp_workspace: Path) -> None:
    """A basic pack includes the file path and useful content."""
    result = build_worker_context_pack(
        tmp_workspace,
        files=["aura/config.py"],
        goal="Test goal",
        spec="Test spec",
        acceptance="Test acceptance",
    )
    assert "Worker Context Pack" in result
    assert "aura/config.py" in result


def test_max_chars_respected(tmp_workspace: Path) -> None:
    """Setting max_chars=50 limits the output length."""
    result = build_worker_context_pack(
        tmp_workspace,
        files=["aura/config.py"],
        goal="Test goal",
        spec="Test spec",
        acceptance="Test acceptance",
        max_chars=50,
    )
    assert len(result) <= 50


def test_missing_file_caveat(tmp_workspace: Path) -> None:
    """A missing file produces a caveat without crashing."""
    result = build_worker_context_pack(
        tmp_workspace,
        files=["nonexistent.py"],
        goal="Test goal",
        spec="Test spec",
        acceptance="Test acceptance",
    )
    assert result  # non-empty
    assert "(missing)" in result or "not found" in result


def test_none_workspace_returns_empty() -> None:
    """workspace_root=None returns an empty string."""
    result = build_worker_context_pack(
        None,
        files=["aura/config.py"],
        goal="Test goal",
        spec="Test spec",
        acceptance="Test acceptance",
    )
    assert result == ""


def test_validation_commands_appear(tmp_workspace: Path) -> None:
    """Validation commands appear in the result."""
    result = build_worker_context_pack(
        tmp_workspace,
        files=["aura/config.py"],
        goal="Test goal",
        spec="Test spec",
        acceptance="Test acceptance",
        validation_commands=["python -m pytest tests/"],
    )
    assert "Validation Commands" in result
    assert "python -m pytest tests/" in result


def test_deterministic(tmp_workspace: Path) -> None:
    """Same inputs produce identical output."""
    kwargs = dict(
        workspace_root=tmp_workspace,
        files=["aura/config.py"],
        goal="Test goal",
        spec="Test spec",
        acceptance="Test acceptance",
    )
    result1 = build_worker_context_pack(**kwargs)
    result2 = build_worker_context_pack(**kwargs)
    assert result1 == result2


def test_empty_files_returns_empty(tmp_workspace: Path) -> None:
    """An empty files list returns an empty string."""
    result = build_worker_context_pack(
        tmp_workspace,
        files=[],
        goal="Test goal",
        spec="Test spec",
        acceptance="Test acceptance",
    )
    assert result == ""


def test_multiple_files(tmp_workspace: Path) -> None:
    """Multiple files all appear in the result."""
    result = build_worker_context_pack(
        tmp_workspace,
        files=["aura/config.py", "README.md", "scripts/smoke.py"],
        goal="Test goal",
        spec="Test spec",
        acceptance="Test acceptance",
    )
    assert "aura/config.py" in result
    assert "README.md" in result
    assert "scripts/smoke.py" in result
