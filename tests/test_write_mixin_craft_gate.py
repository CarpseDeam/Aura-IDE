"""Focused tests for the direct Craft gate in write tools."""

from unittest.mock import MagicMock, patch

import pytest

from aura.conversation.tools._types import ApprovalDecision, ToolExecResult
from aura.conversation.tools._write_mixin import WriteHandlersMixin, _run_craft_gate
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.task_shape import TaskShape
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


def _new_tool_shape() -> TaskShape:
    return TaskShape(
        task_kind="new_tool_or_app",
        product_flow=["configure/create the thing", "run the main action"],
        state_concepts=["job/task", "source/input", "result/candidate"],
        craft_pressure=["block placeholder/demo/fake production code"],
    )


class TestWriteMixinCraftGate:
    def test_worker_registry_stores_task_shape(self, tmp_workspace):
        registry = ToolRegistry(tmp_workspace, mode="worker")
        shape = _new_tool_shape()

        registry.set_task_shape(shape)

        assert registry.get_task_shape() is shape

    @pytest.mark.usefixtures("enable_craft")
    def test_craft_gate_passes_task_shape_to_capsule(self, tmp_workspace):
        shape = _new_tool_shape()
        captured = {}

        class CapturingEngine:
            def process_proposal(self, capsule):
                captured["task_shape"] = capsule.task_shape
                return CraftDecision(approved=True, cleaned_code=capsule.proposed_code)

        proposal = {
            "ok": True,
            "rel_path": "new.py",
            "old_content": "",
            "new_content": "value = 1\n",
            "is_new_file": True,
        }

        with patch("aura.conversation.tools._write_mixin.CraftEngine", new=lambda: CapturingEngine()):
            result = _run_craft_gate(proposal, "write_file", workspace_root=tmp_workspace, task_shape=shape)

        assert result is None
        assert captured["task_shape"] is shape
        assert proposal["craft_metadata"]["task_shape"]["task_kind"] == "new_tool_or_app"

    def test_write_handler_passes_registry_task_shape_to_craft(self, tmp_workspace):
        reg = DummyWriteRegistry(tmp_workspace)
        shape = _new_tool_shape()
        reg.set_task_shape(shape)
        approve_cb = _approve()

        with patch("aura.conversation.tools._write_mixin._run_craft_gate") as mock_craft:
            mock_craft.return_value = None
            result = _handler("write_file")(
                reg,
                {"path": "new.py", "content": "value = 1\n"},
                approve_cb,
                False,
            )

        assert result.ok is True
        assert mock_craft.call_args.kwargs["task_shape"] is shape

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
    def test_new_tool_blocks_placeholder_comments_and_bodies(self, tmp_workspace):
        proposal = {
            "ok": True,
            "rel_path": "app.py",
            "old_content": "",
            "new_content": (
                "# TODO: implement real loading\n"
                "def load_candidates():\n"
                "    pass\n\n"
                "def run_job():\n"
                "    raise NotImplementedError()\n"
            ),
            "is_new_file": True,
        }

        result = _run_craft_gate(
            proposal,
            "write_file",
            workspace_root=tmp_workspace,
            task_shape=_new_tool_shape(),
        )

        assert result is not None
        codes = {issue["code"] for issue in result.payload["craft_issues"]}
        assert "task-shape-placeholder-comment" in codes
        assert "task-shape-placeholder-body" in codes

    @pytest.mark.usefixtures("enable_craft")
    def test_new_tool_blocks_placeholder_production_names(self, tmp_workspace):
        proposal = {
            "ok": True,
            "rel_path": "service.py",
            "old_content": "",
            "new_content": (
                "class FakeAdapter:\n"
                "    def connect(self):\n"
                "        return True\n\n"
                "class SourceAdapter:\n"
                "    def fetch(self):\n"
                "        return []\n\n"
                "def demo_loader():\n"
                "    return []\n"
            ),
            "is_new_file": True,
        }

        result = _run_craft_gate(
            proposal,
            "write_file",
            workspace_root=tmp_workspace,
            task_shape=_new_tool_shape(),
        )

        assert result is not None
        codes = {issue["code"] for issue in result.payload["craft_issues"]}
        assert "task-shape-placeholder-name" in codes
        assert "task-shape-fake-integration-stub" in codes

    @pytest.mark.usefixtures("enable_craft")
    def test_new_tool_allows_mock_names_in_test_files(self, tmp_workspace):
        proposal = {
            "ok": True,
            "rel_path": "tests/test_service.py",
            "old_content": "",
            "new_content": (
                "class MockSource:\n"
                "    def candidates(self):\n"
                "        return ['one']\n"
            ),
            "is_new_file": True,
        }

        result = _run_craft_gate(
            proposal,
            "write_file",
            workspace_root=tmp_workspace,
            task_shape=_new_tool_shape(),
        )

        assert result is None

    @pytest.mark.usefixtures("enable_craft")
    def test_new_tool_generic_manager_name_is_warning_when_real(self, tmp_workspace):
        proposal = {
            "ok": True,
            "rel_path": "jobs.py",
            "old_content": "",
            "new_content": (
                "class JobManager:\n"
                "    def __init__(self):\n"
                "        self.history = []\n\n"
                "    def record(self, job_id):\n"
                "        self.history.append(job_id)\n"
                "        return list(self.history)\n"
            ),
            "is_new_file": True,
        }

        result = _run_craft_gate(
            proposal,
            "write_file",
            workspace_root=tmp_workspace,
            task_shape=_new_tool_shape(),
        )

        assert result is None
        warnings = proposal.get("craft_warnings") or []
        assert any(issue["code"] == "task-shape-generic-scaffold-name" for issue in warnings)

    @pytest.mark.usefixtures("enable_craft")
    def test_new_tool_empty_generic_manager_is_blocked(self, tmp_workspace):
        proposal = {
            "ok": True,
            "rel_path": "jobs.py",
            "old_content": "",
            "new_content": "class JobManager:\n    pass\n",
            "is_new_file": True,
        }

        result = _run_craft_gate(
            proposal,
            "write_file",
            workspace_root=tmp_workspace,
            task_shape=_new_tool_shape(),
        )

        assert result is not None
        codes = {issue["code"] for issue in result.payload["craft_issues"]}
        assert "task-shape-ceremonial-generic-class" in codes or "task-shape-empty-class" in codes

    @pytest.mark.usefixtures("enable_craft")
    def test_useful_new_tool_code_passes_craft(self, tmp_workspace):
        proposal = {
            "ok": True,
            "rel_path": "release_tracker.py",
            "old_content": "",
            "new_content": (
                "from dataclasses import dataclass, field\n\n"
                "@dataclass\n"
                "class ReleaseJob:\n"
                "    source: str\n"
                "    candidates: list[str] = field(default_factory=list)\n"
                "    errors: list[str] = field(default_factory=list)\n\n"
                "    def record_candidate(self, version):\n"
                "        if version not in self.candidates:\n"
                "            self.candidates.append(version)\n"
                "        return self.candidates\n\n"
                "def review_release(job):\n"
                "    if job.errors:\n"
                "        return {'state': 'partial_failure', 'errors': job.errors}\n"
                "    if not job.candidates:\n"
                "        return {'state': 'empty', 'message': 'No releases found'}\n"
                "    return {'state': 'ready', 'latest': job.candidates[-1]}\n"
            ),
            "is_new_file": True,
        }

        result = _run_craft_gate(
            proposal,
            "write_file",
            workspace_root=tmp_workspace,
            task_shape=_new_tool_shape(),
        )

        assert result is None

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
