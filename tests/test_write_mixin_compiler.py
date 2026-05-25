"""Focused tests for Craft compiler integration in write tools."""

import pytest
from unittest.mock import patch, MagicMock

from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.tools._write_mixin import WriteHandlersMixin, _run_compiler_pipeline
from aura.conversation.tools._types import ToolExecResult
from aura.craft.types import CompilerBounce, CraftIssue, CraftIssueSeverity


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
        elif name == "edit_file":
            return registry._handle_edit_file(args, cb, reject_all)
        elif name == "edit_symbol":
            return registry._handle_edit_symbol(args, cb, reject_all)
    return _run


@pytest.fixture
def enable_craft(monkeypatch):
    """Enable AURA_CRAFT for compiler tests."""
    monkeypatch.setenv("AURA_CRAFT", "1")


class TestWriteMixinCompiler:

    @pytest.mark.usefixtures("enable_craft")
    def test_compiler_bounce_becomes_quality_bounce_result(self, tmp_workspace):
        proposal = {
            "ok": True,
            "rel_path": "a.py",
            "old_content": "value = 1\n",
            "new_content": "value = missing\n",
            "is_new_file": False,
        }
        issue = CraftIssue(
            line=1,
            column=8,
            code="undefined-name",
            message="Name 'missing' is used but never defined.",
            suggestion="Define or import the name before using it.",
            severity=CraftIssueSeverity.HARD,
        )

        with patch("aura.conversation.tools._write_mixin.compiler_service.process_proposal") as process:
            process.side_effect = lambda capsule, workspace_root=None: CompilerBounce(
                capsule=capsule,
                issues=[issue],
                repair_instructions="Define missing before use.",
                attempt_number=1,
                max_attempts=2,
            )

            result = _run_compiler_pipeline(proposal, "edit_file", workspace_root=tmp_workspace)

        assert result is not None
        assert result.ok is True
        assert result.payload["ok"] is True
        assert result.payload["applied"] is False
        assert result.payload["quality_bounce"] is True
        assert result.payload["path"] == "a.py"
        assert result.payload["tool_name"] == "edit_file"
        assert result.payload["repair_instructions"] == "Define missing before use."
        assert result.payload["craft_issues"][0]["code"] == "undefined-name"

    @pytest.mark.usefixtures("enable_craft")
    def test_new_python_write_enters_craft(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock()
        
        with patch("aura.conversation.tools._write_mixin._reg.propose_write") as mock_pw, \
             patch("aura.conversation.tools._write_mixin._run_compiler_pipeline") as mock_craft:
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "new.py",
                "old_content": "",
                "new_content": "print('hello')",
                "is_new_file": True
            }
            mock_craft.return_value = None
            
            _handler("write_file")(
                reg, {"path": "new.py", "content": "print('hello')"}, approve_cb, False
            )
            
            mock_craft.assert_called_once()
            args = mock_craft.call_args[0]
            assert args[0]["rel_path"] == "new.py"
            assert args[1] == "write_file"

    @pytest.mark.usefixtures("enable_craft")
    def test_existing_python_write_enters_craft(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock()
        
        with patch("aura.conversation.tools._write_mixin._reg.propose_write") as mock_pw, \
             patch("aura.conversation.tools._write_mixin._run_compiler_pipeline") as mock_craft:
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old",
                "new_content": "new",
                "is_new_file": False
            }
            mock_craft.return_value = None
            
            _handler("write_file")(
                reg, {"path": "existing.py", "content": "new"}, approve_cb, False
            )
            
            mock_craft.assert_called_once()
            args = mock_craft.call_args[0]
            assert args[0]["rel_path"] == "existing.py"

    @pytest.mark.usefixtures("enable_craft")
    def test_existing_python_write_compiler_bounce(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock()
        
        with patch("aura.conversation.tools._write_mixin._reg.propose_write") as mock_pw, \
             patch("aura.conversation.tools._write_mixin._run_compiler_pipeline") as mock_craft:
            mock_pw.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old",
                "new_content": "bad_syntax",
                "is_new_file": False
            }
            mock_craft.return_value = ToolExecResult(
                ok=True,
                payload={
                    "ok": True,
                    "applied": False,
                    "quality_bounce": True,
                    "repair_instructions": "Syntax error",
                    "path": "existing.py",
                },
            )
            
            result = _handler("write_file")(
                reg, {"path": "existing.py", "content": "bad_syntax"}, approve_cb, False
            )
            
            assert result.ok is True
            assert result.payload["quality_bounce"] is True
            assert result.payload["applied"] is False
            assert result.payload["repair_instructions"] == "Syntax error"
            approve_cb.assert_not_called()

    @pytest.mark.usefixtures("enable_craft")
    def test_edit_file_enters_craft(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock()
        
        with patch("aura.conversation.tools._write_mixin._reg.propose_edit") as mock_pe, \
             patch("aura.conversation.tools._write_mixin._run_compiler_pipeline") as mock_craft:
            mock_pe.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old",
                "new_content": "new",
                "is_new_file": False
            }
            mock_craft.return_value = None
            
            _handler("edit_file")(
                reg, {"path": "existing.py", "old_str": "old", "new_str": "new"}, approve_cb, False
            )
            
            mock_craft.assert_called_once()
            args = mock_craft.call_args[0]
            assert args[0]["rel_path"] == "existing.py"
            assert args[1] == "edit_file"

    @pytest.mark.usefixtures("enable_craft")
    def test_edit_symbol_enters_craft(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        approve_cb = MagicMock()
        
        with patch("aura.conversation.tools._write_mixin._reg.propose_edit_symbol") as mock_pes, \
             patch("aura.conversation.tools._write_mixin._run_compiler_pipeline") as mock_craft:
            mock_pes.return_value = {
                "ok": True,
                "rel_path": "existing.py",
                "old_content": "old",
                "new_content": "new",
                "is_new_file": False
            }
            mock_craft.return_value = None
            
            _handler("edit_symbol")(
                reg, {"path": "existing.py", "symbol_type": "function", "symbol_name": "foo", "new_definition": "def foo(): pass"}, approve_cb, False
            )
            
            mock_craft.assert_called_once()
            args = mock_craft.call_args[0]
            assert args[0]["rel_path"] == "existing.py"
            assert args[1] == "edit_symbol"

    @pytest.mark.usefixtures("enable_craft")
    def test_invalidation_on_approved_write_integration(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        
        class MockApprove:
            action = "approve"
            metadata = {}
        approve_cb = MagicMock(return_value=MockApprove())
        
        # Write 1: Add a symbol to constants.py
        res1 = _handler("write_file")(
            reg, {"path": "constants.py", "content": "MY_SYMBOL = 42\n"}, approve_cb, False
        )
        assert res1.ok
        
        # Write 2: Use that symbol in another file
        res2 = _handler("write_file")(
            reg, {"path": "main.py", "content": "from constants import MY_SYMBOL\nprint(MY_SYMBOL)\n"}, approve_cb, False
        )
        assert res2.ok
        assert res2.payload.get("bounce") is not True

    @pytest.mark.usefixtures("enable_craft")
    def test_rejected_writes_do_not_invalidate(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        
        class MockReject:
            action = "reject"
            metadata = {}
        approve_cb = MagicMock(return_value=MockReject())
        
        with patch("aura.conversation.tools._write_mixin.compiler_service.invalidate_workspace_index") as mock_inval:
            res = _handler("write_file")(
                reg, {"path": "constants.py", "content": "MY_SYMBOL = 42\n"}, approve_cb, False
            )
            assert not res.ok
            mock_inval.assert_not_called()

    @pytest.mark.usefixtures("enable_craft")
    def test_compiler_bounces_do_not_invalidate(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        
        class MockApprove:
            action = "approve"
            metadata = {}
        approve_cb = MagicMock(return_value=MockApprove())
        
        with patch("aura.conversation.tools._write_mixin.compiler_service.invalidate_workspace_index") as mock_inval:
            res = _handler("write_file")(
                reg, {"path": "main.py", "content": "import does_not_exist\n"}, approve_cb, False
            )
            assert res.ok
            assert res.payload.get("quality_bounce") is True
            assert res.payload.get("applied") is False
            mock_inval.assert_not_called()

    @pytest.mark.usefixtures("enable_craft")
    def test_reject_all_does_not_invalidate(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        
        class MockApprove:
            action = "approve"
            metadata = {}
        approve_cb = MagicMock(return_value=MockApprove())
        
        with patch("aura.conversation.tools._write_mixin.compiler_service.invalidate_workspace_index") as mock_inval:
            res = _handler("write_file")(
                reg, {"path": "main.py", "content": "MY_SYMBOL = 42\n"}, approve_cb, True
            )
            assert not res.ok
            assert res.extras.get("rejected_all") is True
            mock_inval.assert_not_called()
