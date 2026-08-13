"""Built-in file mutations immediately refresh the shared CodeIntel index."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import aura.code_intel  # noqa: F401 -- triggers adapter registration
from aura.conversation.tools.registry import ToolRegistry

write_mixin = importlib.import_module("aura.conversation.tools._write_mixin")


class _Decision:
    def __init__(self, action: str) -> None:
        self.action = action
        self.metadata: dict[str, str] = {}


def _approve(_request: object) -> _Decision:
    return _Decision("approve")


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(workspace_root=root)


def _symbol_names(registry: ToolRegistry, path: str) -> list[str]:
    return [symbol.name for symbol in registry._code_intel_index.get_symbols(path)]


def test_successful_write_refreshes_the_shared_code_intel_path(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def old_name(): pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry._code_intel_index.refresh(changed_files=["app.py"])

    result = registry._handle_write_file(
        {"path": "app.py", "content": "def new_name(): pass\n"},
        _approve,
        False,
    )

    assert result.ok is True
    assert _symbol_names(registry, "app.py") == ["new_name"]


def test_non_applied_writes_do_not_refresh_code_intel(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def old_name(): pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry._code_intel_index.refresh(changed_files=["app.py"])

    with patch.object(
        registry,
        "_refresh_code_intel_paths",
        wraps=registry._refresh_code_intel_paths,
    ) as refresh:
        rejected = registry._handle_write_file(
            {"path": "app.py", "content": "def rejected_name(): pass\n"},
            lambda _request: _Decision("reject"),
            False,
        )
        invalid = registry._handle_write_file(
            {"path": "app.py", "content": "def broken(:\n"},
            _approve,
            False,
        )

    assert rejected.ok is False
    assert invalid.ok is False
    refresh.assert_not_called()
    assert _symbol_names(registry, "app.py") == ["old_name"]


def test_stale_write_approval_does_not_refresh_code_intel(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def old_name(): pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry._code_intel_index.refresh(changed_files=["app.py"])

    def approve_after_external_change(_request: object) -> _Decision:
        target.write_text("def external_change(): pass\n", encoding="utf-8")
        return _Decision("approve")

    with patch.object(
        registry,
        "_refresh_code_intel_paths",
        wraps=registry._refresh_code_intel_paths,
    ) as refresh:
        result = registry._handle_write_file(
            {"path": "app.py", "content": "def approved_name(): pass\n"},
            approve_after_external_change,
            False,
        )

    assert result.ok is False
    assert result.payload["failure_class"] == "stale_approval"
    refresh.assert_not_called()
    assert _symbol_names(registry, "app.py") == ["old_name"]


def test_successful_delete_evicts_cached_code_intel_facts(tmp_path: Path) -> None:
    target = tmp_path / "gone.py"
    target.write_text("def gone_soon(): pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry._code_intel_index.refresh(changed_files=["gone.py"])
    assert _symbol_names(registry, "gone.py") == ["gone_soon"]

    result = registry._handle_delete_file({"path": "gone.py"}, _approve, False)

    assert result.ok is True
    assert registry._code_intel_index.get_file("gone.py") is None
    assert registry._code_intel_index.get_symbols("gone.py") == []


def test_successful_single_file_patch_refreshes_its_target(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def old_name(): pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry._code_intel_index.refresh(changed_files=["app.py"])

    result = registry._handle_patch_file(
        {"path": "app.py", "edits": [{"old": "old_name", "new": "new_name"}]},
        _approve,
        False,
    )

    assert result.ok is True
    assert _symbol_names(registry, "app.py") == ["new_name"]


def test_successful_multi_file_patch_refreshes_every_committed_target(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "nested" / "second.py"
    second.parent.mkdir()
    first.write_text("def old_first(): pass\n", encoding="utf-8")
    second.write_text("def old_second(): pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry._code_intel_index.refresh(changed_files=["first.py", "nested/second.py"])

    result = registry._handle_patch_file(
        {
            "files": [
                {
                    "path": "first.py",
                    "edits": [{"old": "old_first", "new": "new_first"}],
                },
                {
                    "path": "nested/second.py",
                    "edits": [{"old": "old_second", "new": "new_second"}],
                },
            ]
        },
        _approve,
        False,
    )

    assert result.ok is True
    assert _symbol_names(registry, "first.py") == ["new_first"]
    assert _symbol_names(registry, "nested/second.py") == ["new_second"]


def test_clean_patch_rollback_refreshes_only_temporarily_written_paths(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("def old_first(): pass\n", encoding="utf-8")
    second.write_text("def old_second(): pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry._code_intel_index.refresh(changed_files=["first.py", "second.py"])
    real_atomic_write = write_mixin._atomic_write_bytes

    def fail_second_write(target: Path, content: bytes) -> None:
        if target == second:
            raise OSError("simulated disk failure")
        real_atomic_write(target, content)

    monkeypatch.setattr(write_mixin, "_atomic_write_bytes", fail_second_write)
    with patch.object(
        registry,
        "_refresh_code_intel_paths",
        wraps=registry._refresh_code_intel_paths,
    ) as refresh:
        result = registry._handle_patch_file(
            {
                "files": [
                    {
                        "path": "first.py",
                        "edits": [{"old": "old_first", "new": "new_first"}],
                    },
                    {
                        "path": "second.py",
                        "edits": [{"old": "old_second", "new": "new_second"}],
                    },
                ]
            },
            _approve,
            False,
        )

    assert result.ok is False
    assert result.payload["rolled_back"] is True
    refresh.assert_called_once_with(["first.py"])
    assert _symbol_names(registry, "first.py") == ["old_first"]
    assert _symbol_names(registry, "second.py") == ["old_second"]


def test_partial_patch_rollback_reconciles_possibly_changed_paths(
    tmp_path: Path, monkeypatch
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("def old_first(): pass\n", encoding="utf-8")
    second.write_text("def old_second(): pass\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry._code_intel_index.refresh(changed_files=["first.py", "second.py"])
    real_atomic_write = write_mixin._atomic_write_bytes

    def fail_second_write_and_rollback(target: Path, content: bytes) -> None:
        if target == first and b"new_first" in content:
            real_atomic_write(target, content)
            return
        raise OSError("simulated rollback failure")

    monkeypatch.setattr(
        write_mixin,
        "_atomic_write_bytes",
        fail_second_write_and_rollback,
    )
    with patch.object(
        registry,
        "_refresh_code_intel_paths",
        wraps=registry._refresh_code_intel_paths,
    ) as refresh:
        result = registry._handle_patch_file(
            {
                "files": [
                    {
                        "path": "first.py",
                        "edits": [{"old": "old_first", "new": "new_first"}],
                    },
                    {
                        "path": "second.py",
                        "edits": [{"old": "old_second", "new": "new_second"}],
                    },
                ]
            },
            _approve,
            False,
        )

    assert result.ok is False
    assert result.payload["workspace_state"] == "potentially_partial"
    refresh.assert_called_once_with(["first.py"])
    assert _symbol_names(registry, "first.py") == ["new_first"]
    assert _symbol_names(registry, "second.py") == ["old_second"]


def test_diagnostic_scratch_writes_follow_code_intel_canonical_policy(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)

    result = registry._handle_write_file(
        {
            "path": ".aura/tmp/diagnostic_probe.py",
            "content": "def scratch_only(): pass\n",
        },
        _approve,
        False,
    )

    assert result.ok is True
    assert registry._code_intel_index.get_file(".aura/tmp/diagnostic_probe.py") is None
