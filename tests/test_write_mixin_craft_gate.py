"""Focused tests for the direct Craft gate in write tools."""

from unittest.mock import MagicMock, patch

import pytest

from aura.conversation.tools._types import ApprovalDecision, ToolExecResult
from aura.conversation.tools._write_mixin import WriteHandlersMixin, _run_craft_gate
from aura.conversation.tools.registry import ToolRegistry
from aura.craft.types import CraftDecision, CraftIssue, CraftIssueSeverity


class DummyWriteRegistry(ToolRegistry, WriteHandlersMixin):
    def __init__(self, root, mode="normal", read_only=False):
        self._root = root
        self._mode = mode
        self._read_only = read_only

    def _resolve_in_root(self, path):
        return self._root / path

    def get_contract(self):
        return None


def _handler(name):
    def _run(registry, args, cb, reject_all):
        if name == "write_file":
            return registry._handle_write_file(args, cb, reject_all)
        if name == "apply_edit_transaction":
            return registry._handle_apply_edit_transaction(args, cb, reject_all)
        if name == "edit_file":
            return registry._handle_edit_file(args, cb, reject_all)
        if name == "edit_symbol":
            return registry._handle_edit_symbol(args, cb, reject_all)
        raise AssertionError(f"unsupported handler: {name}")
    return _run


@pytest.fixture
def enable_craft(monkeypatch):
    monkeypatch.setenv("AURA_CRAFT", "1")


def _approve():
    return MagicMock(return_value=ApprovalDecision("approve"))


class TestWriteMixinCraftGate:
    @pytest.mark.usefixtures("enable_craft")
    def test_craft_gate_blocks_before_approval(self, tmp_workspace):
        proposal = {
            "ok": True,
            "rel_path": "a.py",
            "old_content": "value = 1\n",
            "new_content": "def placeholder():\n    pass\n",
            "is_new_file": False,
        }

        result = _run_craft_gate(proposal, "edit_file", workspace_root=tmp_workspace)

        assert result is not None
        assert result.ok is False
        assert result.payload["applied"] is False
        assert result.payload["write_outcome"] == "not_applied_craft_rejected"
        assert result.payload["failure_class"] == "craft_blocked"
        assert result.payload["craft_issues"][0]["code"] in {"stub-body-pass", "demo-scaffolding"}
        assert "Compiler" not in str(result.payload)
        assert "quality_bounce" not in result.payload

    @pytest.mark.usefixtures("enable_craft")
    def test_craft_gate_applies_cleaned_code_to_proposal(self, tmp_workspace):
        proposal = {
            "ok": True,
            "rel_path": "a.py",
            "old_content": "",
            "new_content": "# Initialize value\nvalue = 1\n",
            "is_new_file": True,
        }

        result = _run_craft_gate(proposal, "write_file", workspace_root=tmp_workspace)

        assert result is None
        assert proposal["new_content"] == "value = 1\n"
        assert proposal["write_outcome"] == "applied"

    @pytest.mark.usefixtures("enable_craft")
    def test_write_file_runs_craft_before_approval_with_cleaned_content(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = _approve()

        result = _handler("write_file")(
            reg,
            {"path": "new.py", "content": "# Initialize value\nvalue = 1\n"},
            approve_cb,
            False,
        )

        assert result.ok is True
        assert approve_cb.call_args.args[0].new_content == "value = 1\n"
        assert (tmp_workspace / "new.py").read_text(encoding="utf-8") == "value = 1\n"

    @pytest.mark.usefixtures("enable_craft")
    def test_write_file_craft_block_does_not_request_approval_or_write(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = _approve()

        result = _handler("write_file")(
            reg,
            {"path": "new.py", "content": "def todo():\n    pass\n"},
            approve_cb,
            False,
        )

        assert result.ok is False
        assert result.payload["failure_class"] == "craft_blocked"
        assert result.payload["applied"] is False
        assert not (tmp_workspace / "new.py").exists()
        approve_cb.assert_not_called()

    @pytest.mark.usefixtures("enable_craft")
    def test_apply_edit_transaction_enters_craft(self, tmp_workspace):
        target = tmp_workspace / "existing.py"
        target.write_text("value = 1\n", encoding="utf-8")
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = _approve()

        with patch("aura.conversation.tools._write_mixin._run_craft_gate") as mock_craft:
            mock_craft.return_value = None
            _handler("apply_edit_transaction")(
                reg,
                {
                    "path": "existing.py",
                    "operations": [
                        {"op": "replace_text_once", "old": "value = 1\n", "new": "value = 2\n"}
                    ],
                },
                approve_cb,
                False,
            )

        mock_craft.assert_called_once()
        assert mock_craft.call_args.args[1] == "apply_edit_transaction"

    @pytest.mark.usefixtures("enable_craft")
    def test_edit_file_enters_craft(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock()

        with patch("aura.conversation.tools._write_mixin._reg.propose_edit") as mock_pe, \
             patch("aura.conversation.tools._write_mixin._run_craft_gate") as mock_craft:
            mock_pe.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old",
                "new_content": "new",
                "is_new_file": False,
            }
            mock_craft.return_value = None

            _handler("edit_file")(
                reg, {"path": "existing.py", "old_str": "old", "new_str": "new"}, approve_cb, False
            )

        mock_craft.assert_called_once()
        assert mock_craft.call_args.args[1] == "edit_file"

    @pytest.mark.usefixtures("enable_craft")
    def test_craft_block_from_handler_returns_without_approval(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock()
        blocked = ToolExecResult(
            ok=False,
            payload={
                "ok": False,
                "applied": False,
                "path": "existing.py",
                "failure_class": "craft_blocked",
                "write_outcome": "not_applied_craft_rejected",
            },
        )

        with patch("aura.conversation.tools._write_mixin._reg.propose_write") as mock_pw, \
             patch("aura.conversation.tools._write_mixin._run_craft_gate", return_value=blocked):
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old",
                "new_content": "def todo():\n    pass\n",
                "is_new_file": False,
            }

            result = _handler("write_file")(
                reg, {"path": "existing.py", "content": "def todo():\n    pass\n"}, approve_cb, False
            )

        assert result.payload["failure_class"] == "craft_blocked"
        approve_cb.assert_not_called()

    @pytest.mark.usefixtures("enable_craft")
    def test_edit_symbol_enters_craft(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock()

        with patch("aura.conversation.tools._write_mixin._reg.propose_edit_symbol") as mock_pes, \
             patch("aura.conversation.tools._write_mixin._run_craft_gate") as mock_craft:
            mock_pes.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old",
                "new_content": "new",
                "is_new_file": False,
            }
            mock_craft.return_value = None

            _handler("edit_symbol")(
                reg,
                {
                    "path": "existing.py",
                    "symbol_type": "function",
                    "symbol_name": "foo",
                    "new_definition": "def foo():\n    return 1\n",
                },
                approve_cb,
                False,
            )

        mock_craft.assert_called_once()
        assert mock_craft.call_args.args[1] == "edit_symbol"

    @pytest.mark.usefixtures("enable_craft")
    def test_rejected_writes_do_not_apply(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock(return_value=ApprovalDecision("reject"))

        result = _handler("write_file")(
            reg, {"path": "constants.py", "content": "MY_SYMBOL = 42\n"}, approve_cb, False
        )

        assert result.ok is False
        assert result.payload["applied"] is False
        assert not (tmp_workspace / "constants.py").exists()

    @pytest.mark.usefixtures("enable_craft")
    def test_reject_all_does_not_apply(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock(return_value=ApprovalDecision("approve"))

        result = _handler("write_file")(
            reg, {"path": "main.py", "content": "MY_SYMBOL = 42\n"}, approve_cb, True
        )

        assert result.ok is False
        assert result.extras.get("rejected_all") is True
        assert not (tmp_workspace / "main.py").exists()
