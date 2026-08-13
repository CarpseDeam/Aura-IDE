"""``write_file``/``patch_file`` schema descriptions must match production.

These tests prove the model-facing write-tool schemas describe only real
runtime capabilities and constraints: no stale multi-mode wording, and no
parameters that carry a documented rule the handlers don't actually enforce.
"""

from __future__ import annotations

from pathlib import Path

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.tools.schemas.write import FILESYSTEM_WRITE_TOOL_DEFS

_APPROVE = lambda _req: ApprovalDecision(action="approve")  # noqa: E731

_STALE_MODE_WORDS = ("multiple modes", "separate execution role")


def _tool_def(name: str) -> dict:
    for tool in FILESYSTEM_WRITE_TOOL_DEFS:
        if tool["function"]["name"] == name:
            return tool
    raise AssertionError(f"{name} not found in FILESYSTEM_WRITE_TOOL_DEFS")


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


def test_write_tool_schemas_contain_no_stale_multi_mode_wording() -> None:
    for name in ("write_file", "delete_file", "patch_file"):
        text = _all_description_text(_tool_def(name))
        for word in _STALE_MODE_WORDS:
            assert word not in text, f"{name} schema still mentions {word!r}"


def test_write_file_schema_has_no_unenforced_replacement_parameters() -> None:
    props = _tool_def("write_file")["function"]["parameters"]["properties"]
    assert "full_replace_existing" not in props
    assert "replacement_reason" not in props


def test_write_file_overwrites_an_existing_file_with_only_path_and_content(
    tmp_path: Path,
) -> None:
    """The schema no longer claims full_replace_existing/replacement_reason
    are required to overwrite — prove the handler actually agrees."""
    target = tmp_path / "a.py"
    target.write_text("old = 1\n", encoding="utf-8")
    reg = ToolRegistry(workspace_root=tmp_path)

    result = reg._handle_write_file(
        {"path": "a.py", "content": "new = 2\n"},
        _APPROVE,
        False,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert target.read_text(encoding="utf-8") == "new = 2\n"


def test_patch_file_schema_does_not_discourage_write_file_recovery() -> None:
    text = _all_description_text(_tool_def("patch_file"))
    assert "recovery" not in text
    assert "failure recovery" not in text


def test_patch_file_expected_file_hash_is_optional_in_both_call_shapes() -> None:
    patch_def = _tool_def("patch_file")["function"]["parameters"]
    assert "expected_file_hash" not in patch_def.get("required", [])

    files_item_schema = patch_def["properties"]["files"]["items"]
    assert "expected_file_hash" not in files_item_schema.get("required", [])
