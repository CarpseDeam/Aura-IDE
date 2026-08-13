"""Multi-file ``patch_file`` transactions.

``patch_file`` upgrades from one-file-multi-hunk to one-or-more-files-in-one
reviewed transaction. These tests cover the invariants that make that
upgrade safe:

* the legacy ``path``/``edits`` payload keeps working, normalized internally
  into a one-file transaction (no second write pipeline);
* every file's proposal is built entirely in memory before approval, and a
  failure anywhere — an ambiguous hunk, invalid Python, a duplicate path —
  fails the whole transaction with nothing written;
* one ``ApprovalRequest`` represents the complete transaction, so the user
  makes exactly one decision;
* the live commit re-verifies every target against its approved snapshot,
  and rolls back whatever was already written if a later file fails.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from aura.conversation.tools._types import ApprovalFileChange, ApprovalRequest
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.tools.write_transaction import (
    FileProposal,
    PatchTransaction,
    commit_patch_transaction,
)


class _Decision:
    def __init__(self, action: str) -> None:
        self.action = action
        self.note = ""
        self.metadata: dict[str, Any] = {}


def _approve(req: ApprovalRequest) -> _Decision:
    return _Decision("approve")


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(workspace_root=root)


# ---------------------------------------------------------------------------
# 1. legacy single-file patch still works
# ---------------------------------------------------------------------------


def test_legacy_single_file_patch_still_applies(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
        _approve,
        False,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert result.payload["file_count"] == 1
    assert target.read_text(encoding="utf-8") == "x = 2\n"


# ---------------------------------------------------------------------------
# 2. two-file transaction applies both files
# 3. multiple hunks across multiple files
# ---------------------------------------------------------------------------


def test_two_file_transaction_applies_both_with_multiple_hunks_each(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "sub" / "b.py"
    b.parent.mkdir(parents=True)
    a.write_text("x = 1\ny = 2\n", encoding="utf-8")
    b.write_text("p = 10\nq = 20\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {
            "files": [
                {
                    "path": "a.py",
                    "edits": [
                        {"old": "x = 1", "new": "x = 100"},
                        {"old": "y = 2", "new": "y = 200"},
                    ],
                },
                {
                    "path": "sub/b.py",
                    "edits": [
                        {"old": "p = 10", "new": "p = 1000"},
                        {"old": "q = 20", "new": "q = 2000"},
                    ],
                },
            ],
            "description": "bump constants",
        },
        _approve,
        False,
    )

    assert result.ok is True
    assert result.payload["file_count"] == 2
    assert a.read_text(encoding="utf-8") == "x = 100\ny = 200\n"
    assert b.read_text(encoding="utf-8") == "p = 1000\nq = 2000\n"
    edit_counts = {f["path"]: f["edit_count"] for f in result.payload["files"]}
    assert edit_counts == {"a.py": 2, "sub/b.py": 2}


# ---------------------------------------------------------------------------
# 4. failure in file 2 proposal leaves file 1 untouched
# ---------------------------------------------------------------------------


def test_proposal_failure_in_second_file_leaves_first_file_untouched(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 2\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 999"}]},
                {"path": "b.py", "edits": [{"old": "not present anywhere", "new": "z = 1"}]},
            ],
        },
        _approve,
        False,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    # Nothing reached disk for either file — proposal construction is
    # entirely in-memory.
    assert a.read_text(encoding="utf-8") == "x = 1\n"
    assert b.read_text(encoding="utf-8") == "y = 2\n"
    assert result.payload.get("rel_path") == "b.py" or result.payload.get("path") == "b.py"


# ---------------------------------------------------------------------------
# 5. ambiguous hunk anywhere rejects whole transaction
# ---------------------------------------------------------------------------


def test_ambiguous_hunk_in_any_file_rejects_whole_transaction(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("dup = 1\ndup = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
                {"path": "b.py", "edits": [{"old": "dup = 1", "new": "dup = 2"}]},
            ],
        },
        _approve,
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "patch_hunk_ambiguous"
    assert a.read_text(encoding="utf-8") == "x = 1\n"
    assert b.read_text(encoding="utf-8") == "dup = 1\ndup = 1\n"


# ---------------------------------------------------------------------------
# 6. duplicate paths rejected
# ---------------------------------------------------------------------------


def test_duplicate_target_paths_are_rejected(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    a.write_text("x = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 3"}]},
            ],
        },
        _approve,
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "patch_transaction_duplicate_path"
    assert a.read_text(encoding="utf-8") == "x = 1\n"


def test_path_and_files_together_is_rejected(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    a.write_text("x = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {
            "path": "a.py",
            "edits": [{"old": "x = 1", "new": "x = 2"}],
            "files": [{"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 3"}]}],
        },
        _approve,
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "patch_transaction_invalid_form"
    assert a.read_text(encoding="utf-8") == "x = 1\n"


def test_neither_path_nor_files_is_rejected(tmp_path: Path) -> None:
    reg = _registry(tmp_path)

    result = reg._handle_patch_file({"description": "no target"}, _approve, False)

    assert result.ok is False
    assert result.payload["failure_class"] == "patch_transaction_invalid_form"


# ---------------------------------------------------------------------------
# 7. Python syntax-invalid candidate in one file rejects whole transaction
# ---------------------------------------------------------------------------


def test_syntax_invalid_candidate_in_one_file_rejects_whole_transaction(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("def f():\n    return 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
                {
                    "path": "b.py",
                    "edits": [{"old": "    return 1", "new": "    return (("}],
                },
            ],
        },
        _approve,
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "patch_candidate_invalid_syntax"
    assert a.read_text(encoding="utf-8") == "x = 1\n"
    assert b.read_text(encoding="utf-8") == "def f():\n    return 1\n"


# ---------------------------------------------------------------------------
# 8. one approval request represents the complete transaction
# ---------------------------------------------------------------------------


def test_one_approval_request_represents_the_complete_transaction(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_bytes(b"x = 1\n")
    b.write_bytes(b"y = 1\n")
    reg = _registry(tmp_path)

    seen_requests: list[ApprovalRequest] = []

    def capturing_approve(req: ApprovalRequest) -> _Decision:
        seen_requests.append(req)
        return _Decision("approve")

    reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
                {"path": "b.py", "edits": [{"old": "y = 1", "new": "y = 2"}]},
            ],
        },
        capturing_approve,
        False,
    )

    assert len(seen_requests) == 1
    req = seen_requests[0]
    assert req.is_multi_file is True
    assert [c.rel_path for c in req.file_changes] == ["a.py", "b.py"]
    assert req.file_changes[0].new_content == "x = 2\n"
    assert req.file_changes[1].new_content == "y = 2\n"


# ---------------------------------------------------------------------------
# 9. rejecting transaction changes no files
# ---------------------------------------------------------------------------


def test_rejecting_transaction_changes_no_files(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
                {"path": "b.py", "edits": [{"old": "y = 1", "new": "y = 2"}]},
            ],
        },
        lambda req: _Decision("reject"),
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "approval_rejected"
    assert a.read_text(encoding="utf-8") == "x = 1\n"
    assert b.read_text(encoding="utf-8") == "y = 1\n"


def test_reject_all_changes_no_files(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
                {"path": "b.py", "edits": [{"old": "y = 1", "new": "y = 2"}]},
            ],
        },
        lambda req: _Decision("reject_all"),
        False,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert a.read_text(encoding="utf-8") == "x = 1\n"
    assert b.read_text(encoding="utf-8") == "y = 1\n"


# ---------------------------------------------------------------------------
# 10. one stale target after approval prevents all writes
# ---------------------------------------------------------------------------


def test_one_stale_target_after_approval_prevents_all_writes(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    def approving_but_mutates_b(req: ApprovalRequest) -> _Decision:
        # b.py changes on disk after the transaction was proposed and shown,
        # but before the (single) approval decision lands.
        b.write_text("y = 999\n", encoding="utf-8")
        return _Decision("approve")

    result = reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
                {"path": "b.py", "edits": [{"old": "y = 1", "new": "y = 2"}]},
            ],
        },
        approving_but_mutates_b,
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "stale_approval"
    assert result.payload["path"] == "b.py"
    # Neither file was written — a itself was never touched even though it
    # was not the stale one.
    assert a.read_text(encoding="utf-8") == "x = 1\n"
    assert b.read_text(encoding="utf-8") == "y = 999\n"


# ---------------------------------------------------------------------------
# 11. successful transaction preserves expected contents/newlines
# ---------------------------------------------------------------------------


def test_successful_transaction_preserves_crlf_newlines(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    a.write_bytes(b"x = 1\r\ny = 2\r\n")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 100"}]},
        _approve,
        False,
    )

    assert result.ok is True
    assert a.read_bytes() == b"x = 100\r\ny = 2\r\n"


# ---------------------------------------------------------------------------
# 12. simulated failure during second filesystem replacement rolls back file 1
# 13. rollback failure is reported truthfully as potentially partial
# ---------------------------------------------------------------------------


def _proposal(target: Path, rel_path: str, old: str, new: str) -> FileProposal:
    return FileProposal(
        target=target,
        rel_path=rel_path,
        old_content=old,
        new_content=new,
        hunk_count=1,
        target_snapshot={
            "exists": True,
            "is_file": True,
            "raw_content_hash": None,
            "size": len(old.encode("utf-8")),
        },
    )


def test_commit_failure_on_second_file_rolls_back_first_file(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 1\n", encoding="utf-8")

    transaction = PatchTransaction(
        files=(
            _proposal(a, "a.py", "x = 1\n", "x = 2\n"),
            _proposal(b, "b.py", "y = 1\n", "y = 2\n"),
        )
    )

    real_write = __import__(
        "aura.conversation.tools.fs_write", fromlist=["atomic_write_bytes"]
    ).atomic_write_bytes

    def flaky_write(target: Path, data: bytes) -> None:
        if target == b:
            raise OSError("simulated disk failure writing b.py")
        real_write(target, data)

    result = commit_patch_transaction(
        tmp_path,
        transaction,
        capture_before_write=lambda rel: None,
        atomic_write_bytes=flaky_write,
    )

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["rolled_back"] is True
    assert result["failure_class"] == "patch_transaction_commit_failed"
    # a.py was written then rolled back to its exact original bytes.
    assert a.read_text(encoding="utf-8") == "x = 1\n"
    assert b.read_text(encoding="utf-8") == "y = 1\n"


def test_rollback_failure_is_reported_as_potentially_partial(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 1\n", encoding="utf-8")

    transaction = PatchTransaction(
        files=(
            _proposal(a, "a.py", "x = 1\n", "x = 2\n"),
            _proposal(b, "b.py", "y = 1\n", "y = 2\n"),
        )
    )

    real_write = __import__(
        "aura.conversation.tools.fs_write", fromlist=["atomic_write_bytes"]
    ).atomic_write_bytes

    def always_fails_after_first(target: Path, data: bytes) -> None:
        if target == a and data == b"x = 2\n":
            real_write(target, data)
            return
        # Both the second file's write AND the rollback-write of a.py fail.
        raise OSError("simulated total disk failure")

    result = commit_patch_transaction(
        tmp_path,
        transaction,
        capture_before_write=lambda rel: None,
        atomic_write_bytes=always_fails_after_first,
    )

    assert result["ok"] is False
    assert result["rolled_back"] is False
    assert result["failure_class"] == "transaction_rollback_failed"
    assert result["workspace_state"] == "potentially_partial"
    assert result["applied"] == "partial"
    # a.py was left in its (unexpectedly) applied state — rollback failed and
    # the result says so rather than claiming a clean workspace.
    assert a.read_text(encoding="utf-8") == "x = 2\n"


# ---------------------------------------------------------------------------
# 14. restore-point/backup integration covers every changed path
# ---------------------------------------------------------------------------


def test_restore_point_hook_and_backups_cover_every_changed_path(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    captured: list[str] = []

    class _FakeRestorePointManager:
        def capture_path(self, rel_path: str) -> None:
            captured.append(rel_path)

    reg.set_restore_point_manager(_FakeRestorePointManager())

    result = reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
                {"path": "b.py", "edits": [{"old": "y = 1", "new": "y = 2"}]},
            ],
        },
        _approve,
        False,
    )

    assert result.ok is True
    assert set(captured) == {"a.py", "b.py"}

    backups_root = tmp_path / ".aura" / "backups"
    backup_files = {p.name for p in backups_root.rglob("*.py")}
    assert backup_files == {"a.py", "b.py"}
    for entry in result.payload["files"]:
        assert entry["backup"] is not None
        assert (tmp_path / entry["backup"]).read_text(encoding="utf-8") in {"x = 1\n", "y = 1\n"}


# ---------------------------------------------------------------------------
# 15. transaction result reports every changed file
# ---------------------------------------------------------------------------


def test_transaction_result_reports_every_changed_file(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    c = tmp_path / "c.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("y = 1\n", encoding="utf-8")
    c.write_text("z = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_patch_file(
        {
            "files": [
                {"path": "a.py", "edits": [{"old": "x = 1", "new": "x = 2"}]},
                {"path": "b.py", "edits": [{"old": "y = 1", "new": "y = 2"}]},
                {"path": "c.py", "edits": [{"old": "z = 1", "new": "z = 2"}]},
            ],
        },
        _approve,
        False,
    )

    assert result.ok is True
    assert result.payload["file_count"] == 3
    reported_paths = {f["path"] for f in result.payload["files"]}
    assert reported_paths == {"a.py", "b.py", "c.py"}


# ---------------------------------------------------------------------------
# 18. production tool catalog gains no new editing tool
# ---------------------------------------------------------------------------


def test_no_new_editing_tool_was_added_to_the_catalog(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    names = {d["function"]["name"] for d in reg.tool_defs()}
    mutation_names = {"write_file", "patch_file", "delete_file"}
    assert mutation_names <= names
    # No new general-purpose text-patching tool exists alongside patch_file:
    # only one name in the whole catalog contains "patch".
    patch_like = {n for n in names if "patch" in n.lower()}
    assert patch_like == {"patch_file"}
    banned_names = {"patch_files", "apply_edit_transaction", "edit_symbol"}
    assert not (banned_names & names)


# ---------------------------------------------------------------------------
# 19. patch_file effect classification remains unchanged
# ---------------------------------------------------------------------------


def test_patch_file_effect_classification_is_unchanged(tmp_path: Path) -> None:
    from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect

    assert BUILTIN_TOOL_EFFECTS["patch_file"] is ToolEffect.MUTATION


# ---------------------------------------------------------------------------
# 16 & 17: single-file approval UI behavior remains valid; multi-file diff
# rendering contains every path and diff.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def test_single_file_dialog_rendering_is_unchanged(qapp) -> None:
    from aura.gui.diff_dialog import DiffApprovalDialog

    req = ApprovalRequest(
        tool_name="patch_file",
        rel_path="a.py",
        old_content="x = 1\n",
        new_content="x = 2\n",
        is_new_file=False,
    )
    dlg = DiffApprovalDialog(req)
    try:
        assert dlg.windowTitle() == "Apply change to a.py?"
        assert "-x = 1" in dlg._diff_view.toPlainText()
        assert "+x = 2" in dlg._diff_view.toPlainText()
    finally:
        dlg.deleteLater()


def test_multi_file_dialog_shows_every_path_and_diff(qapp) -> None:
    from aura.gui.diff_dialog import DiffApprovalDialog

    changes = (
        ApprovalFileChange("a.py", "x = 1\n", "x = 2\n", False),
        ApprovalFileChange("sub/b.py", "y = 1\n", "y = 2\n", False),
    )
    req = ApprovalRequest(
        tool_name="patch_file",
        rel_path=changes[0].rel_path,
        old_content=changes[0].old_content,
        new_content=changes[0].new_content,
        is_new_file=False,
        changes=changes,
    )
    dlg = DiffApprovalDialog(req)
    try:
        assert "2 files" in dlg.windowTitle()
        text = dlg._diff_view.toPlainText()
        assert "a.py" in text
        assert "sub/b.py" in text
        assert "-x = 1" in text and "+x = 2" in text
        assert "-y = 1" in text and "+y = 2" in text
    finally:
        dlg.deleteLater()
