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
    assert payload["applied"] is True
    assert payload["applied_tool"] == "apply_edit_transaction"
    assert payload["operation_count"] == 3
    content = target.read_text(encoding="utf-8")
    assert "return 2" in content
    assert "return 'hello'" in content
    assert "def omega()" in content


def test_same_file_text_operations_use_updated_in_memory_buffer(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_bytes(b"alpha\nbeta\n")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {"op": "replace_text_once", "old": "alpha", "new": "gamma"},
            {"op": "replace_text_once", "old": "gamma", "new": "delta"},
        ],
    )

    assert proposal["ok"] is True
    assert proposal["new_content"] == "delta\nbeta\n"
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_stale_exact_text_failure_reports_not_found_details(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_text("current\n", encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_text_once", "old": "stale", "new": "updated"}],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_not_applicable"
    assert proposal["operation_index"] == 0
    assert proposal["failed_operation"]["old"] == "stale"
    assert proposal["reason"] == "not_found"
    assert proposal["not_found"] is True
    assert proposal["ambiguous"] is False
    assert proposal["candidate_count"] == 0


def test_ambiguous_old_text_failure_reports_candidate_count(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_text("item\nitem\n", encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_text_once", "old": "item", "new": "done"}],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_ambiguous_symbol"
    assert proposal["reason"] == "ambiguous"
    assert proposal["ambiguous"] is True
    assert proposal["candidate_count"] == 2
    assert len(proposal["candidates"]) == 2


def test_newline_normalization_recovery_succeeds_only_when_unique(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_bytes(b"one\r\ntwo\r\n")

    unique = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_text_once", "old": "one\ntwo", "new": "1\n2"}],
    )

    assert unique["ok"] is True
    assert unique["new_content"] == "1\r\n2\r\n"

    target.write_bytes(b"one\r\ntwo\r\none\r\ntwo\r\n")
    ambiguous = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_text_once", "old": "one\ntwo", "new": "1\n2"}],
    )

    assert ambiguous["ok"] is False
    assert ambiguous["ambiguous"] is True
    assert ambiguous["candidate_count"] == 2


def test_trimmed_whitespace_recovery_succeeds_only_when_unique(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_bytes(b"\tvalue = 1\n")

    unique = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_text_once", "old": "    value = 1\n", "new": "value = 2\n"}],
    )

    assert unique["ok"] is True
    assert unique["new_content"] == "value = 2\n"

    target.write_bytes(b"\tvalue = 1\n  value = 1\n")
    ambiguous = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_text_once", "old": "    value = 1\n", "new": "value = 2\n"}],
    )

    assert ambiguous["ok"] is False
    assert ambiguous["ambiguous"] is True
    assert ambiguous["candidate_count"] == 2


def test_surrounding_context_recovery_requires_unique_context(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_bytes(b"before unique\nold current\nafter unique\n")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_text_once",
                "old": "old stale",
                "new": "old fixed\n",
                "before": "before unique\n",
                "after": "after unique",
            }
        ],
    )

    assert proposal["ok"] is True
    assert proposal["new_content"] == "before unique\nold fixed\nafter unique\n"


def test_same_file_operation_conflict_fails_before_writing(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    original = "alpha\nbeta\n"
    target.write_text(original, encoding="utf-8")

    registry = ToolRegistry(tmp_workspace, mode="worker")
    result = TOOL_HANDLERS["apply_edit_transaction"](
        registry,
        {
            "path": "sample.txt",
            "operations": [
                {"op": "replace_text_once", "old": "alpha", "new": "gamma"},
                {"op": "replace_text_once", "old": "alpha", "new": "delta"},
            ],
        },
        _approve,
        False,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert result.payload["failed_operation"]["old"] == "alpha"
    assert target.read_text(encoding="utf-8") == original


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


def test_replace_method_resolves_unique_unqualified_method(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "class Overlay:\n"
        "    def paint(self):\n"
        "        return 'old'\n"
        "\n"
        "class Tray:\n"
        "    def show(self):\n"
        "        return 'tray'\n",
        encoding="utf-8",
    )

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_method",
                "method_name": "paint",
                "new_definition": "def paint(self):\n    return 'new'",
            }
        ],
    )

    assert proposal["ok"] is True
    assert "return 'new'" in proposal["new_content"]


def test_replace_method_accepts_fully_qualified_method_name(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "class Overlay:\n"
        "    def paint(self):\n"
        "        return 'overlay'\n"
        "\n"
        "class Tray:\n"
        "    def paint(self):\n"
        "        return 'tray'\n",
        encoding="utf-8",
    )

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_method",
                "method_name": "Overlay.paint",
                "new_definition": "def paint(self):\n    return 'new overlay'",
            }
        ],
    )

    assert proposal["ok"] is True
    assert "return 'new overlay'" in proposal["new_content"]
    assert "return 'tray'" in proposal["new_content"]


def test_replace_method_missing_unqualified_method_returns_available_symbols(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "class Overlay:\n"
        "    def paint(self):\n"
        "        return 'overlay'\n",
        encoding="utf-8",
    )

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_method",
                "method_name": "missing",
                "new_definition": "def missing(self):\n    return 'new'",
            }
        ],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_symbol_not_found"
    assert proposal["available_symbols"]["methods"] == ["Overlay.paint"]


def test_replace_method_ambiguous_unqualified_method_returns_candidates(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "class Overlay:\n"
        "    def paint(self):\n"
        "        return 'overlay'\n"
        "\n"
        "class Tray:\n"
        "    def paint(self):\n"
        "        return 'tray'\n",
        encoding="utf-8",
    )

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_method",
                "method_name": "paint",
                "new_definition": "def paint(self):\n    return 'new'",
            }
        ],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_ambiguous_symbol"
    assert proposal["candidates"] == ["Overlay.paint", "Tray.paint"]


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


def test_insert_after_method_resolves_unique_unqualified_method(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "class Overlay:\n"
        "    def paint(self):\n"
        "        return 'old'\n",
        encoding="utf-8",
    )

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "insert_after_symbol",
                "symbol_type": "method",
                "method_name": "paint",
                "content": "\n    def erase(self):\n        return 'erase'",
            }
        ],
    )

    assert proposal["ok"] is True
    assert "def erase(self):" in proposal["new_content"]


def test_insert_after_method_accepts_fully_qualified_method_name(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "class Overlay:\n"
        "    def paint(self):\n"
        "        return 'overlay'\n"
        "\n"
        "class Tray:\n"
        "    def paint(self):\n"
        "        return 'tray'\n",
        encoding="utf-8",
    )

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "insert_after_symbol",
                "symbol_type": "method",
                "method_name": "Tray.paint",
                "content": "\n    def hide(self):\n        return 'hide'",
            }
        ],
    )

    assert proposal["ok"] is True
    new_content = proposal["new_content"]
    assert new_content.index("def hide") > new_content.index("return 'tray'")
    assert new_content.index("def hide") > new_content.index("class Tray")


def test_insert_after_method_missing_returns_available_symbols(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "class Overlay:\n"
        "    def paint(self):\n"
        "        return 'overlay'\n",
        encoding="utf-8",
    )

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "insert_after_symbol",
                "symbol_type": "method",
                "method_name": "missing",
                "content": "\n    def hide(self):\n        return 'hide'",
            }
        ],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_symbol_not_found"
    assert proposal["available_symbols"]["methods"] == ["Overlay.paint"]


def test_insert_after_method_ambiguous_returns_candidates(tmp_workspace: Path):
    target = tmp_workspace / "sample.py"
    target.write_text(
        "class Overlay:\n"
        "    def paint(self):\n"
        "        return 'overlay'\n"
        "\n"
        "class Tray:\n"
        "    def paint(self):\n"
        "        return 'tray'\n",
        encoding="utf-8",
    )

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "insert_after_symbol",
                "symbol_type": "method",
                "method_name": "paint",
                "content": "\n    def hide(self):\n        return 'hide'",
            }
        ],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_ambiguous_symbol"
    assert proposal["candidates"] == ["Overlay.paint", "Tray.paint"]


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


def test_replace_text_once_ambiguous_suggests_occurrence_or_allow_multiple(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_text("value\nvalue\n", encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [{"op": "replace_text_once", "old": "value", "new": "changed"}],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_ambiguous_symbol"
    assert proposal["occurrence_count"] == 2
    assert "occurrence" in proposal["suggested_next_action"]
    assert "allow_multiple" in proposal["suggested_next_action"]


def test_replace_text_once_occurrence_replaces_one_based_match(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_text("value\nvalue\nvalue\n", encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_text_once",
                "old": "value",
                "new": "changed",
                "occurrence": 2,
            }
        ],
    )

    assert proposal["ok"] is True
    assert proposal["new_content"].replace("\r\n", "\n") == "value\nchanged\nvalue\n"


def test_replace_text_once_allow_multiple_replaces_all_matches(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_text("value\nvalue\n", encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_text_once",
                "old": "value",
                "new": "changed",
                "allow_multiple": True,
            }
        ],
    )

    assert proposal["ok"] is True
    assert proposal["new_content"].replace("\r\n", "\n") == "changed\nchanged\n"


def test_replace_text_once_occurrence_bounds_are_validated(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_text("value\nvalue\n", encoding="utf-8")

    proposal = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_text_once",
                "old": "value",
                "new": "changed",
                "occurrence": 3,
            }
        ],
    )

    assert proposal["ok"] is False
    assert proposal["failure_class"] == "edit_transaction_invalid_operation"
    assert proposal["occurrence_count"] == 2


def test_replace_text_once_option_types_are_validated(tmp_workspace: Path):
    target = tmp_workspace / "sample.txt"
    target.write_text("value\nvalue\n", encoding="utf-8")

    bad_occurrence = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_text_once",
                "old": "value",
                "new": "changed",
                "occurrence": True,
            }
        ],
    )
    bad_allow_multiple = propose_edit_transaction(
        tmp_workspace,
        target,
        [
            {
                "op": "replace_text_once",
                "old": "value",
                "new": "changed",
                "allow_multiple": "yes",
            }
        ],
    )

    assert bad_occurrence["ok"] is False
    assert bad_occurrence["failure_class"] == "edit_transaction_invalid_operation"
    assert bad_allow_multiple["ok"] is False
    assert bad_allow_multiple["failure_class"] == "edit_transaction_invalid_operation"


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
