"""``read_file`` is the single model-facing read tool, in two call shapes.

Phase 2C.1 folded the standalone ``read_files`` tool into ``read_file``:
``path`` (optionally with ``offset``/``limit``) for one file, or ``paths``
for several known files gathered in one evidence batch. These tests pin the
dispatch-level contract: both shapes work, invalid combinations fail
cleanly, every requested batch path keeps its metadata, and the old
standalone tool is gone.
"""
from __future__ import annotations

from pathlib import Path

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.fs_handler import FsReadHandler
from aura.conversation.tools.registry import ToolRegistry

_APPROVE = lambda _req: ApprovalDecision(action="approve")  # noqa: E731


def _handler(root: Path) -> FsReadHandler:
    registry = ToolRegistry(workspace_root=root)
    return registry._fs_handler  # noqa: SLF001 — exercising the real internal handler


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


# ── single-file shape still works ───────────────────────────────────────────


def test_single_path_reads_whole_file(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "one\ntwo\nthree\n")
    result = _handler(tmp_path).handle_read_file({"path": "a.py"})

    assert result["ok"] is True
    assert result["content"] == "one\ntwo\nthree\n"


def test_single_path_with_offset_and_limit_reads_a_window(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "one\ntwo\nthree\nfour\n")
    result = _handler(tmp_path).handle_read_file({"path": "a.py", "offset": 2, "limit": 2})

    assert result["ok"] is True
    assert result["content"] == "two\nthree\n"


# ── invalid combinations fail cleanly ───────────────────────────────────────


def test_neither_path_nor_paths_fails_cleanly(tmp_path: Path) -> None:
    result = _handler(tmp_path).handle_read_file({})

    assert result["ok"] is False
    assert "path" in result["error"]


def test_both_path_and_paths_fails_cleanly(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "x\n")
    result = _handler(tmp_path).handle_read_file({"path": "a.py", "paths": ["a.py"]})

    assert result["ok"] is False
    assert "exactly one" in result["error"]


def test_offset_with_paths_fails_cleanly(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "x\n")
    result = _handler(tmp_path).handle_read_file({"paths": ["a.py"], "offset": 1})

    assert result["ok"] is False
    assert "offset" in result["error"]


def test_empty_paths_array_fails_cleanly(tmp_path: Path) -> None:
    result = _handler(tmp_path).handle_read_file({"paths": []})

    assert result["ok"] is False


# ── batch shape retrieves many known files in one call ─────────────────────


def test_paths_retrieves_at_least_eight_known_files_in_one_call(tmp_path: Path) -> None:
    names = [f"module_{i}.py" for i in range(8)]
    for name in names:
        _write(tmp_path / name, f"VALUE_{name} = 1\n")

    result = _handler(tmp_path).handle_read_file({"paths": names})

    assert result["ok"] is True
    assert set(result["files"].keys()) == set(names)
    for name in names:
        entry = result["files"][name]
        assert entry["ok"] is True
        assert entry["status"] == "complete"
        assert f"VALUE_{name}" in entry["content"]


def test_every_requested_batch_path_keeps_metadata_including_errors(tmp_path: Path) -> None:
    _write(tmp_path / "real.py", "x = 1\n")

    result = _handler(tmp_path).handle_read_file(
        {"paths": ["real.py", "does_not_exist.py"]}
    )

    assert result["ok"] is True
    real = result["files"]["real.py"]
    assert real["ok"] is True
    assert real["status"] == "complete"
    assert real["content_hash"]

    missing = result["files"]["does_not_exist.py"]
    assert missing["ok"] is False
    assert missing["status"] == "error"
    assert missing["reason"]


def test_a_large_file_in_a_batch_reports_omission_or_summary_not_silent_loss(
    tmp_path: Path,
) -> None:
    from aura.conversation.tools.fs_handler import READ_FILES_FULL_INCLUDE_CHARS

    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * (READ_FILES_FULL_INCLUDE_CHARS // 6 + 200), encoding="utf-8")

    result = _handler(tmp_path).handle_read_file({"paths": ["big.py"]})
    entry = result["files"]["big.py"]

    assert entry["ok"] is True
    assert entry["status"] in ("summarized", "truncated")
    assert entry["truncated"] is True
    assert entry["continuation"]


# ── the standalone read_files tool is fully gone ────────────────────────────


def test_read_files_tool_name_is_not_dispatchable(tmp_path: Path) -> None:
    from aura.conversation.tools.registry import TOOL_HANDLERS

    assert "read_files" not in TOOL_HANDLERS

    registry = ToolRegistry(workspace_root=tmp_path)
    result = registry.execute(
        "read_files", {"paths": ["a.py"]}, approval_cb=_APPROVE,
    )
    assert result.ok is False


def test_read_file_paths_shape_dispatches_through_the_registry(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "a = 1\n")
    _write(tmp_path / "b.py", "b = 1\n")
    registry = ToolRegistry(workspace_root=tmp_path)

    result = registry.execute(
        "read_file", {"paths": ["a.py", "b.py"]}, approval_cb=_APPROVE,
    )

    assert result.ok is True
    assert set(result.payload["files"].keys()) == {"a.py", "b.py"}
