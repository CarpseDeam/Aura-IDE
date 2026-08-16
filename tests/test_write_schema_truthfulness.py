"""``apply_patch``'s schema description must match production.

These tests prove the model-facing consolidated mutation-tool schema
describes only real runtime capabilities and constraints: no stale
multi-mode wording, and no parameters that carry a documented rule the
handlers don't actually enforce.
"""

from __future__ import annotations

from pathlib import Path

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.tools.schemas.write import APPLY_PATCH_TOOL_DEF

_APPROVE = lambda _req: ApprovalDecision(action="approve")  # noqa: E731

_STALE_MODE_WORDS = ("multiple modes", "separate execution role")


def _all_description_text(tool: dict) -> str:
    """Flatten every 'description' string anywhere in a tool def, lowercased."""
    chunks: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "description" and isinstance(value, str):
                    chunks.append(value)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(tool)
    return " ".join(chunks).lower()


def test_apply_patch_schema_contains_no_stale_multi_mode_wording() -> None:
    text = _all_description_text(APPLY_PATCH_TOOL_DEF)
    for word in _STALE_MODE_WORDS:
        assert word not in text, f"apply_patch schema still mentions {word!r}"


def test_apply_patch_schema_has_no_unenforced_replacement_parameters() -> None:
    props = APPLY_PATCH_TOOL_DEF["function"]["parameters"]["properties"]
    assert "full_replace_existing" not in props
    assert "replacement_reason" not in props


def test_apply_patch_schema_declares_an_operation_enum() -> None:
    params = APPLY_PATCH_TOOL_DEF["function"]["parameters"]
    operation = params["properties"]["operation"]
    assert set(operation["enum"]) == {"create", "replace", "patch", "delete"}
    assert "operation" in params["required"]


def test_write_file_overwrites_an_existing_file_with_only_path_and_content(
    tmp_path: Path,
) -> None:
    """The schema no longer claims full_replace_existing/replacement_reason
    are required to overwrite — prove the handler actually agrees."""
    target = tmp_path / "a.py"
    target.write_text("old = 1\n", encoding="utf-8")
    reg = ToolRegistry(workspace_root=tmp_path)

    result = reg._handle_apply_patch(
        {"operation": "replace", "path": "a.py", "content": "new = 2\n"},
        _APPROVE,
        False,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert target.read_text(encoding="utf-8") == "new = 2\n"


def test_apply_patch_schema_does_not_discourage_recovery() -> None:
    text = _all_description_text(APPLY_PATCH_TOOL_DEF)
    assert "recovery" not in text
    assert "failure recovery" not in text


def test_apply_patch_expected_file_hash_is_optional_in_both_call_shapes() -> None:
    params = APPLY_PATCH_TOOL_DEF["function"]["parameters"]
    assert "expected_file_hash" not in params.get("required", [])

    files_item_schema = params["properties"]["files"]["items"]
    assert "expected_file_hash" not in files_item_schema.get("required", [])
