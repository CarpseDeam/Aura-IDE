"""Focused tests for Craft compiler integration in write tools."""

import pytest
from unittest.mock import patch, MagicMock

from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.tools._write_mixin import WriteHandlersMixin
from aura.conversation.tools._types import ToolExecResult


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
                ok=False,
                payload={"ok": False, "error": "Syntax error", "path": "existing.py", "bounce": True}
            )
            
            result = _handler("write_file")(
                reg, {"path": "existing.py", "content": "bad_syntax"}, approve_cb, False
            )
            
            assert result.ok is False
            assert result.payload["bounce"] is True
            assert result.payload["error"] == "Syntax error"
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
