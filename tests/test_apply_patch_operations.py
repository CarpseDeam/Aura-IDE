"""Phase 2C: ``apply_patch`` routes every operation through the existing
write authority -- no parallel write engine.

Offline, no provider/API calls. Drives the real ``ToolRegistry`` against a
temp workspace so every assertion exercises the actual
``_write_mixin.py`` / ``write_transaction.py`` / ``fs_write.py`` owners, not
a fake.
"""
from __future__ import annotations

from pathlib import Path

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry

_APPROVE = lambda _req: ApprovalDecision(action="approve")  # noqa: E731
_REJECT = lambda _req: ApprovalDecision(action="reject")  # noqa: E731


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(workspace_root=root)


# ── operation shape validation happens before any write owner runs ─────────


def test_missing_operation_is_rejected_with_a_focused_correction(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    result = reg.execute("apply_patch", {"path": "x.py", "content": "x = 1"}, _APPROVE)

    assert result.ok is False
    assert result.payload["failure_class"] == "invalid_arguments"
    assert "operation" in result.payload["error"]


def test_create_with_edits_field_is_rejected(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    result = reg.execute(
        "apply_patch",
        {"operation": "create", "path": "x.py", "content": "x = 1", "edits": []},
        _APPROVE,
    )
    assert result.ok is False
    assert result.payload["failure_class"] == "invalid_arguments"


def test_patch_with_content_field_is_rejected(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    result = reg.execute(
        "apply_patch",
        {"operation": "patch", "path": "x.py", "content": "x = 2"},
        _APPROVE,
    )
    assert result.ok is False
    assert result.payload["failure_class"] == "invalid_arguments"


def test_delete_with_content_field_is_rejected(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    result = reg.execute(
        "apply_patch",
        {"operation": "delete", "path": "x.py", "content": "irrelevant"},
        _APPROVE,
    )
    assert result.ok is False
    assert result.payload["failure_class"] == "invalid_arguments"


# ── each operation routes through the real write owner ─────────────────────


def test_create_operation_writes_a_new_file_through_real_write_authority(
    tmp_path: Path,
) -> None:
    reg = _registry(tmp_path)
    result = reg.execute(
        "apply_patch",
        {"operation": "create", "path": "new.py", "content": "x = 1\n"},
        _APPROVE,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert result.payload["is_new_file"] is True
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "x = 1\n"
    # A backup entry is present (None for a brand-new file, but the key
    # exists) -- proof this went through the real backup-aware write owner.
    assert "backup" in result.payload


def test_replace_operation_overwrites_an_existing_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("old = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)
    result = reg.execute(
        "apply_patch",
        {"operation": "replace", "path": "a.py", "content": "new = 2\n"},
        _APPROVE,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert result.payload["is_new_file"] is False
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "new = 2\n"
    # Backed up before being overwritten.
    assert result.payload.get("backup")


def test_patch_operation_applies_exact_text_hunks(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)
    result = reg.execute(
        "apply_patch",
        {
            "operation": "patch",
            "path": "a.py",
            "edits": [{"old": "alpha = 1", "new": "alpha = 2"}],
        },
        _APPROVE,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "alpha = 2\n"


def test_patch_operation_multi_file_transaction_is_atomic(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)
    result = reg.execute(
        "apply_patch",
        {
            "operation": "patch",
            "files": [
                {"path": "a.py", "edits": [{"old": "alpha = 1", "new": "alpha = 2"}]},
                {"path": "b.py", "edits": [{"old": "beta = 1", "new": "beta = 2"}]},
            ],
        },
        _APPROVE,
    )

    assert result.ok is True
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "alpha = 2\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "beta = 2\n"


def test_delete_operation_removes_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "gone.py"
    target.write_text("x = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)
    result = reg.execute(
        "apply_patch",
        {"operation": "delete", "path": "gone.py", "reason": "cleanup"},
        _APPROVE,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert result.payload["deleted"] is True
    assert not target.exists()
    # Backed up before deletion.
    assert result.payload.get("backup")


# ── approval, stale checks, and rejection all still apply ──────────────────


def test_create_rejected_by_the_user_never_applies(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    result = reg.execute(
        "apply_patch",
        {"operation": "create", "path": "new.py", "content": "x = 1\n"},
        _REJECT,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert result.payload["failure_class"] == "approval_rejected"
    assert not (tmp_path / "new.py").exists()


def test_delete_rejected_by_the_user_never_applies(tmp_path: Path) -> None:
    target = tmp_path / "keep.py"
    target.write_text("x = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)
    result = reg.execute(
        "apply_patch",
        {"operation": "delete", "path": "keep.py"},
        _REJECT,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert target.exists()


def test_replace_becomes_stale_when_the_target_changes_during_approval(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.py"
    target.write_text("old = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    def approve_but_race(_req):
        target.write_text("raced = 9\n", encoding="utf-8")
        return ApprovalDecision(action="approve")

    result = reg.execute(
        "apply_patch",
        {"operation": "replace", "path": "a.py", "content": "new = 2\n"},
        approve_but_race,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert result.payload["failure_class"] == "stale_approval"
    assert target.read_text(encoding="utf-8") == "raced = 9\n"


def test_patch_transaction_rolls_back_on_commit_failure(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta = 1\n", encoding="utf-8")
    reg = _registry(tmp_path)

    def approve_and_race(_req):
        # The second file changes underneath the transaction right when
        # approval lands -- the freshness re-check fails the whole thing.
        (tmp_path / "b.py").write_text("raced = 9\n", encoding="utf-8")
        return ApprovalDecision(action="approve")

    result = reg.execute(
        "apply_patch",
        {
            "operation": "patch",
            "files": [
                {"path": "a.py", "edits": [{"old": "alpha = 1", "new": "alpha = 2"}]},
                {"path": "b.py", "edits": [{"old": "beta = 1", "new": "beta = 2"}]},
            ],
        },
        approve_and_race,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    # Neither file's proposed content was ever committed.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "alpha = 1\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "raced = 9\n"


# ── read-only mode still refuses every operation ────────────────────────────


def test_read_only_mode_refuses_apply_patch(tmp_path: Path) -> None:
    reg = ToolRegistry(workspace_root=tmp_path, read_only=True)
    result = reg.execute(
        "apply_patch",
        {"operation": "create", "path": "x.py", "content": "x = 1\n"},
        _APPROVE,
    )
    assert result.ok is False
    assert result.payload["applied"] is False
    assert not (tmp_path / "x.py").exists()


# ── the old per-operation tool names are gone from the catalog ─────────────


def test_legacy_write_tool_names_are_rejected_as_unexposed(tmp_path: Path) -> None:
    import json

    from aura.conversation.tool_preflight import preflight_structural
    from aura.conversation.tools.registry import ToolRegistry as _Registry

    reg = _Registry(workspace_root=tmp_path)
    tool_defs = reg.tool_defs()
    exposed_names = {t["function"]["name"] for t in tool_defs}

    for legacy in ("write_file", "patch_file", "delete_file"):
        assert legacy not in exposed_names

    # And a call using a legacy name is structurally well-formed but not
    # exposed -- exactly the same rejection path preflight uses for any
    # unknown/withheld name.
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "write_file", "arguments": json.dumps({"path": "x", "content": "y"})},
    }
    assert preflight_structural([call]) is None  # structurally fine
    from aura.conversation.tool_preflight import exposed_tool_schemas

    assert "write_file" not in exposed_tool_schemas(tool_defs)
