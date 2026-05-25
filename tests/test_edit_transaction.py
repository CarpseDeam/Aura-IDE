from __future__ import annotations

import hashlib
from pathlib import Path

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.fs_edit_transaction import propose_edit_transaction
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry


def _approve(_req):
    return ApprovalDecision(action="approve")


def test_multi_operation_one_file_python_edit_succeeds_through_one_transaction(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self):\n"
        "        return 'hi'\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(tmp_workspace, mode="worker")

    result = TOOL_HANDLERS["apply_edit_transaction"](
        registry,
        {
            "path": "sample.py",
            "operations": [
                {
                    "op": "replace_function",
                    "symbol_name": "alpha",
                    "new_definition": "def alpha():\n    return 2",
                },
                {
                    "op": "replace_method",
                    "class_name": "Greeter",
                    "symbol_name": "greet",
                    "new_definition": "def greet(self):\n    return 'hello'",
                },
                {
                    "op": "insert_after_symbol",
                    "symbol_type": "class",
                    "symbol_name": "Greeter",
                    "content": "\ndef omega():\n    return 3\n",
                },
            ],
        },
        _approve,
        False,
    )

    payload = result.payload
    assert result.ok is True
    assert payload["applied"] == "apply_edit_transaction"
    assert payload["operation_count"] == 3
    content = target.read_text(encoding="utf-8")
    assert "return 2" in content
    assert "return 'hello'" in content
    assert "def omega()" in content


def test_failed_method_replacement_does_not_write_and_returns_symbol_not_found(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    original = "class Greeter:\n    def greet(self):\n        return 'hi'\n"
    target.write_text(original, encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_method",
                "class_name": "Greeter",
                "symbol_name": "missing",
                "new_definition": "def missing(self):\n    return 'x'",
            }
        ],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_symbol_not_found"
    assert target.read_text(encoding="utf-8") == original


def test_symbol_aliases_are_normalized_by_operation_kind(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    original = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self):\n"
        "        return 'hi'\n"
    )
    target.write_text(original, encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_function",
                "function_name": "alpha",
                "new_definition": "def alpha():\n    return 2",
            },
            {
                "op": "replace_method",
                "class_name": "Greeter",
                "method_name": "greet",
                "new_definition": "def greet(self):\n    return 'hello'",
            },
            {
                "op": "insert_after_symbol",
                "symbol_type": "method",
                "class_name": "Greeter",
                "method_name": "greet",
                "content": "\n    def wave(self):\n        return 'wave'",
            },
        ],
    )

    assert proposal["ok"] is True
    assert "return 2" in proposal["new_content"]
    assert "return 'hello'" in proposal["new_content"]
    assert "def wave(self):" in proposal["new_content"]


def test_replace_method_accepts_method_name_alias(tmp_path: Path):
    path = tmp_path / "graph_items.py"
    path.write_text(
        "class Node:\n"
        "    def paint(self):\n"
        "        return 'old'\n",
        encoding="utf-8",
    )

    result = propose_edit_transaction(
        tmp_path,
        path,
        [
            {
                "op": "replace_method",
                "class_name": "Node",
                "method_name": "paint",
                "new_definition": "def paint(self):\n    return 'new'",
            }
        ],
    )

    assert result["ok"] is True
    assert "return 'new'" in result["new_content"]


def test_replace_function_accepts_function_name_alias(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text("def alpha():\n    return 1\n", encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_function",
                "function_name": "alpha",
                "new_definition": "def alpha():\n    return 2",
            }
        ],
    )

    assert proposal["ok"] is True
    assert "return 2" in proposal["new_content"]


def test_replace_class_accepts_class_name_alias_as_target(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text("class Greeter:\n    pass\n", encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_class",
                "class_name": "Greeter",
                "new_definition": "class Greeter:\n    value = 1",
            }
        ],
    )

    assert proposal["ok"] is True
    assert "value = 1" in proposal["new_content"]


def test_insert_after_method_accepts_method_name_alias(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "class Greeter:\n"
        "    def greet(self):\n"
        "        return 'hi'\n",
        encoding="utf-8",
    )

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "insert_after_symbol",
                "symbol_type": "method",
                "class_name": "Greeter",
                "method_name": "greet",
                "content": "\n    def wave(self):\n        return 'wave'",
            }
        ],
    )

    assert proposal["ok"] is True
    assert "def wave(self):" in proposal["new_content"]


def test_missing_symbol_alias_failure_includes_operation_index(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text("def alpha():\n    return 1\n", encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_function", "new_definition": "def alpha():\n    return 2"}],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_invalid_operation"
    assert proposal["operation_index"] == 0


def test_invalid_generated_python_does_not_write_and_returns_invalid_syntax(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    original = "def alpha():\n    return 1\n"
    target.write_text(original, encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_function", "symbol_name": "alpha", "new_definition": "def alpha(:\n    return 2"}],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_invalid_syntax"
    assert target.read_text(encoding="utf-8") == original


def test_stale_expected_file_hash_rejects_without_write(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    original = "def alpha():\n    return 1\n"
    target.write_text(original, encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_function", "symbol_name": "alpha", "new_definition": "def alpha():\n    return 2"}],
        expected_file_hash=hashlib.sha256(b"stale").hexdigest(),
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_hash_mismatch"
    assert target.read_text(encoding="utf-8") == original


def test_crlf_input_preserves_crlf_after_transaction(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    original = "def alpha():\r\n    return 1\r\n\r\n"
    target.write_bytes(original.encode("utf-8"))
    registry = ToolRegistry(tmp_workspace, mode="worker")

    result = TOOL_HANDLERS["apply_edit_transaction"](
        registry,
        {
            "path": "sample.py",
            "operations": [
                {"op": "replace_function", "symbol_name": "alpha", "new_definition": "def alpha():\n    return 2"}
            ],
        },
        _approve,
        False,
    )

    assert result.ok is True
    written = target.read_bytes().decode("utf-8")
    assert "\r\n" in written
    assert "\n" not in written.replace("\r\n", "")
    assert "\r\r\n" not in written
