"""Stale-approval hardening for writes and deletes.

Every write/delete approval is bound to the exact target snapshot presented to
the user (existence, file type, raw content hash).  Immediately before an
approved write, patch, or delete is applied, the live target is re-verified
against that snapshot.  When it changed, nothing is applied: the result is
``ok: False``, ``applied: False``, a recoverable ``stale_approval`` failure
class, and a directive to re-read and re-propose.
"""

from __future__ import annotations

import hashlib

from aura.conversation.tools.registry import ToolRegistry


def _approval(action: str):
    return lambda req: _Decision(action)


class _Decision:
    def __init__(self, action: str) -> None:
        self.action = action
        self.note = ""
        self.metadata = {}


def _registry(root):
    return ToolRegistry(workspace_root=root)


def test_write_file_applies_when_the_target_is_unchanged(tmp_path) -> None:
    target = tmp_path / "a.py"
    target.write_text("alpha = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_write_file(
        {"path": "a.py", "content": "beta = 2\n"},
        _approval("approve"),
        False,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert target.read_text(encoding="utf-8") == "beta = 2\n"


def test_write_file_is_stale_when_content_changed_during_approval(tmp_path) -> None:
    target = tmp_path / "a.py"
    target.write_text("alpha = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    def approving_cb(req):
        # The file changes on disk while the approval is pending.
        target.write_text("gamma = 3\n", encoding="utf-8")
        return _Decision("approve")

    result = reg._handle_write_file(
        {"path": "a.py", "content": "beta = 2\n"},
        approving_cb,
        False,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert result.payload["failure_class"] == "stale_approval"
    assert result.payload["stale_approval"] is True
    assert result.payload["recoverable"] is True
    # The approved content must NOT have been applied; the concurrent write won.
    assert target.read_text(encoding="utf-8") == "gamma = 3\n"
    assert "Re-read" in result.payload["suggested_next_action"]


def test_new_file_write_is_stale_when_the_file_appears_during_approval(
    tmp_path,
) -> None:
    target = tmp_path / "new.py"
    reg = _registry(tmp_path)

    def approving_cb(req):
        # A file that did not exist when the proposal was built appears now.
        target.write_text("from elsewhere\n", encoding="utf-8")
        return _Decision("approve")

    result = reg._handle_write_file(
        {"path": "new.py", "content": "mine\n"},
        approving_cb,
        False,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert result.payload["failure_class"] == "stale_approval"
    assert target.read_text(encoding="utf-8") == "from elsewhere\n"


def test_patch_file_is_stale_when_content_changed_during_approval(tmp_path) -> None:
    target = tmp_path / "a.py"
    target.write_text("alpha = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)
    current_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    def approving_cb(req):
        target.write_text("gamma = 3\n", encoding="utf-8")
        return _Decision("approve")

    result = reg._handle_patch_file(
        {
            "path": "a.py",
            "edits": [{"old": "alpha = 1", "new": "beta = 2"}],
            "expected_file_hash": current_hash,
        },
        approving_cb,
        False,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert result.payload["failure_class"] == "stale_approval"
    assert target.read_text(encoding="utf-8") == "gamma = 3\n"


def test_delete_file_applies_when_the_target_is_unchanged(tmp_path) -> None:
    target = tmp_path / "gone.py"
    target.write_text("x = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    result = reg._handle_delete_file(
        {"path": "gone.py", "reason": "cleanup"},
        _approval("approve"),
        False,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert result.payload["deleted"] is True
    assert not target.exists()


def test_delete_file_is_stale_when_content_changed_during_approval(tmp_path) -> None:
    target = tmp_path / "gone.py"
    target.write_text("x = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    def approving_cb(req):
        # The file changes while the user reviews the deletion.
        target.write_text("x = 1\n# new important line\n", encoding="utf-8")
        return _Decision("approve")

    result = reg._handle_delete_file(
        {"path": "gone.py", "reason": "cleanup"},
        approving_cb,
        False,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert result.payload["deleted"] is False
    assert result.payload["failure_class"] == "stale_approval"
    # The file must survive; the changed content is not deleted on a stale approval.
    assert target.exists()
    assert "# new important line" in target.read_text(encoding="utf-8")


def test_delete_file_is_stale_when_the_target_was_already_removed(tmp_path) -> None:
    target = tmp_path / "gone.py"
    target.write_text("x = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    def approving_cb(req):
        # Another process already deleted the file while approval was pending.
        target.unlink()
        return _Decision("approve")

    result = reg._handle_delete_file(
        {"path": "gone.py", "reason": "cleanup"},
        approving_cb,
        False,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert result.payload["deleted"] is False
    assert result.payload["failure_class"] == "stale_approval"
    assert not target.exists()
