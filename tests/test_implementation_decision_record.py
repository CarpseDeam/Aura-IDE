"""Tests for the ``record_implementation_decision`` bookkeeping tool.

Covers the handler's normalization and deterministic identity, the tool's
production-catalog exposure and effect classification, and a production-loop
regression proving the tool changes nothing about the normal send loop: no
runtime transition, no catalog change, no forced next action, no thinking-mode
override, just an ordinary tool result appended to History.

``tests/production_loop_harness.py`` imports a module that no longer exists in
this repo (``aura.conversation.task_router``) — pre-existing, unrelated
breakage this task does not chase (see ``tests/test_plan_review_tool_flow.py``
for the same note). The regression below uses the same self-contained
equivalents that file already established for driving a real
``ConversationManager``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aura.client import ContentDelta, Done, Event, ToolCallArgsDelta, ToolCallEnd, ToolCallStart, ToolResult
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.effects import ToolEffect
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry

TOOL_NAME = "record_implementation_decision"


def _valid_args(**overrides) -> dict:
    args = {
        "decision": (
            "MainWindow owns placement of the external edge-tab host; "
            "EdgeTabRail owns only its internal buttons."
        ),
        "targets": ["aura/gui/main_window.py", "aura/gui/edge_tab_rail.py"],
        "basis": [
            "MainWindow constructs the host and owns top-level window geometry.",
            "EdgeTabRail owns tab widgets and signals but not window positioning.",
        ],
        "reconsider_if": [
            "Another component is found that actually owns host positioning.",
        ],
        "validation": "Run the GUI smoke test and confirm the host follows the window.",
    }
    args.update(overrides)
    return args


def _tool_names(tools: list[dict]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        fn = tool.get("function")
        if isinstance(fn, dict):
            names.add(str(fn.get("name") or ""))
    return names


# ── handler ──────────────────────────────────────────────────────────────


class TestHandler:
    def test_valid_input_succeeds(self, tmp_path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path)

        result = registry.execute(TOOL_NAME, _valid_args(), approval_cb=None)

        assert result.ok is True
        assert result.payload["ok"] is True
        assert result.payload["kind"] == "implementation_decision"
        assert result.payload["decision"] == _valid_args()["decision"]
        assert result.payload["targets"] == _valid_args()["targets"]
        assert result.payload["basis"] == _valid_args()["basis"]
        assert result.payload["reconsider_if"] == _valid_args()["reconsider_if"]
        assert result.payload["validation"] == _valid_args()["validation"]
        assert isinstance(result.payload["decision_id"], str)
        assert len(result.payload["decision_id"]) == 64  # sha256 hex digest

    @pytest.mark.parametrize("field", ["decision", "basis", "reconsider_if", "validation"])
    def test_blank_required_strings_fail(self, tmp_path, field) -> None:
        registry = ToolRegistry(workspace_root=tmp_path)
        blank = "" if field in ("decision", "validation") else []

        result = registry.execute(TOOL_NAME, _valid_args(**{field: blank}), approval_cb=None)

        assert result.ok is False
        assert result.payload["ok"] is False
        assert field in result.payload["error"]

    def test_whitespace_only_required_strings_fail(self, tmp_path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path)

        result = registry.execute(
            TOOL_NAME, _valid_args(decision="   ", validation="\t\n"), approval_cb=None
        )

        assert result.ok is False

    def test_blank_list_entries_are_normalized_away(self, tmp_path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path)

        result = registry.execute(
            TOOL_NAME,
            _valid_args(
                targets=["", "  ", "aura/gui/main_window.py"],
                basis=["", "MainWindow owns geometry.", "   "],
            ),
            approval_cb=None,
        )

        assert result.ok is True
        assert result.payload["targets"] == ["aura/gui/main_window.py"]
        assert result.payload["basis"] == ["MainWindow owns geometry."]

    def test_missing_optional_targets_produces_empty_list(self, tmp_path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path)
        args = _valid_args()
        del args["targets"]

        result = registry.execute(TOOL_NAME, args, approval_cb=None)

        assert result.ok is True
        assert result.payload["targets"] == []

    def test_same_normalized_content_produces_same_decision_id(self, tmp_path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path)

        first = registry.execute(TOOL_NAME, _valid_args(), approval_cb=None)
        second = registry.execute(
            TOOL_NAME,
            _valid_args(decision=f"  {_valid_args()['decision']}  "),
            approval_cb=None,
        )

        assert first.payload["decision_id"] == second.payload["decision_id"]

    def test_materially_different_content_produces_different_decision_id(self, tmp_path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path)

        first = registry.execute(TOOL_NAME, _valid_args(), approval_cb=None)
        second = registry.execute(
            TOOL_NAME, _valid_args(decision="A different implementation decision."),
            approval_cb=None,
        )

        assert first.payload["decision_id"] != second.payload["decision_id"]

    def test_decision_id_ignores_call_order_and_process_state(self, tmp_path) -> None:
        """Two independent registries produce identical ids for identical content."""
        registry_a = ToolRegistry(workspace_root=tmp_path)
        registry_b = ToolRegistry(workspace_root=tmp_path)

        result_a = registry_a.execute(TOOL_NAME, _valid_args(), approval_cb=None)
        result_b = registry_b.execute(TOOL_NAME, _valid_args(), approval_cb=None)

        assert result_a.payload["decision_id"] == result_b.payload["decision_id"]

    def test_returned_ordering_is_deterministic(self, tmp_path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path)
        args = _valid_args(
            basis=["third fact", "first fact", "second fact"],
        )

        result = registry.execute(TOOL_NAME, args, approval_cb=None)

        assert result.payload["basis"] == ["third fact", "first fact", "second fact"]

    def test_payload_carries_working_decision_semantics(self, tmp_path) -> None:
        registry = ToolRegistry(workspace_root=tmp_path)

        result = registry.execute(TOOL_NAME, _valid_args(), approval_cb=None)

        semantics = result.payload["semantics"].lower()
        assert "current working implementation decision" in semantics
        assert "supersedes earlier" in semantics


# ── tool surface ─────────────────────────────────────────────────────────


class TestToolSurface:
    def test_writable_production_single_exposes_the_tool(self) -> None:
        tools = ToolCatalog().build_tool_defs(read_only=False)

        assert TOOL_NAME in _tool_names(tools)

    def test_read_only_mode_does_not_expose_the_tool(self) -> None:
        tools = ToolCatalog().build_tool_defs(read_only=True)

        assert TOOL_NAME not in _tool_names(tools)

    def test_classified_bookkeeping(self) -> None:
        assert ToolCatalog().effect_for(TOOL_NAME) is ToolEffect.BOOKKEEPING

    def test_resolves_through_the_normal_static_registry_handler(self) -> None:
        assert TOOL_HANDLERS[TOOL_NAME] is ToolRegistry._handle_record_implementation_decision


# ── no hidden runtime behavior ───────────────────────────────────────────
#
# Self-contained ConversationManager harness (see module docstring for why
# this doesn't reuse tests/production_loop_harness.py).


def _tool_round(calls: list[tuple[str, str, dict]]) -> list[Event]:
    events: list[Event] = []
    tool_calls = []
    for index, (call_id, name, args) in enumerate(calls):
        arguments = json.dumps(args)
        events.append(ToolCallStart(index=index, id=call_id, name=name))
        events.append(ToolCallArgsDelta(index=index, args_chunk=arguments))
        events.append(ToolCallEnd(index=index))
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    events.append(Done(
        finish_reason="tool_calls",
        full_message={"role": "assistant", "content": "", "tool_calls": tool_calls},
    ))
    return events


def _final_round(text: str) -> list[Event]:
    return [
        ContentDelta(text=text),
        Done(finish_reason="stop", full_message={"role": "assistant", "content": text}),
    ]


def _make_workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "file_00.py").write_text("VALUE_00 = 0\n\nTARGET = 0\n", encoding="utf-8")
    return root


class _ScriptedBackend:
    """A model backend that replays a fixed list of rounds, recording requests."""

    def __init__(self, rounds: list[list[Event]]) -> None:
        self._rounds = rounds
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self._rounds):
            return iter(self._rounds[index])
        return iter(_final_round("(script exhausted)"))

    def tool_names(self, index: int) -> list[str]:
        names: list[str] = []
        for tool in self.calls[index].get("tools") or []:
            fn = tool.get("function") if isinstance(tool, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            if name:
                names.append(str(name))
        return names

    def thinking(self, index: int) -> str:
        return str(self.calls[index].get("thinking", ""))


class _Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, ev: Event) -> None:
        self.events.append(ev)

    @property
    def chat_text(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, ContentDelta))

    def results_named(self, name: str) -> list[ToolResult]:
        return [
            e for e in self.events
            if isinstance(e, ToolResult) and e.name == name
        ]


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


def _run(workspace: Path, backend: _ScriptedBackend, isolated_streams: ModelStreamRegistry):
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text("Set TARGET to 1 in file_00.py.")
    registry = ToolRegistry(workspace_root=workspace)
    manager = ConversationManager(history, registry)
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

    recorder = _Recorder()
    manager.send(
        on_event=recorder,
        approval_cb=lambda _req: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking="high",
    )
    return manager, recorder


class TestNoHiddenRuntimeBehavior:
    def test_recording_a_decision_does_not_end_or_redirect_the_turn(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = _make_workspace(tmp_path / "proj")
        backend = _ScriptedBackend([
            _tool_round([("d0", TOOL_NAME, _valid_args())]),
            _tool_round([("r0", "read_file", {"path": "file_00.py"})]),
            _tool_round([("w0", "write_file", {
                "path": "file_00.py", "content": "VALUE_00 = 0\n\nTARGET = 1\n",
            })]),
            _final_round("Set TARGET to 1 in file_00.py."),
        ])

        manager, recorder = _run(workspace, backend, isolated_streams)

        decision_results = recorder.results_named(TOOL_NAME)
        assert len(decision_results) == 1
        assert decision_results[0].ok is True

        # The catalog and thinking mode never moved across rounds.
        names_by_round = [backend.tool_names(i) for i in range(len(backend.calls))]
        assert all(names == names_by_round[0] for names in names_by_round), (
            "the tool catalog changed after recording a decision"
        )
        thinking_values = {backend.thinking(i) for i in range(len(backend.calls))}
        assert len(thinking_values) == 1, "thinking mode changed after recording a decision"

        # The turn continued through ordinary reasoning rounds afterward,
        # rather than stopping or redirecting when the decision was recorded.
        assert len(recorder.results_named("read_file")) == 1
        assert len(recorder.results_named("write_file")) == 1
        assert json.loads(recorder.results_named("write_file")[0].result)["ok"] is True
        assert recorder.chat_text == "Set TARGET to 1 in file_00.py."
        assert (workspace / "file_00.py").read_text(encoding="utf-8") == (
            "VALUE_00 = 0\n\nTARGET = 1\n"
        )

    def test_two_decisions_in_one_turn_remain_truthful_chronological_entries(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = _make_workspace(tmp_path / "proj")
        first_args = _valid_args()
        second_args = _valid_args(
            decision="Revised: EdgeTabRail also owns docking animation state."
        )
        backend = _ScriptedBackend([
            _tool_round([("d0", TOOL_NAME, first_args)]),
            _tool_round([("d1", TOOL_NAME, second_args)]),
            _final_round("Recorded two decisions."),
        ])

        manager, recorder = _run(workspace, backend, isolated_streams)

        decision_results = recorder.results_named(TOOL_NAME)
        assert len(decision_results) == 2
        assert decision_results[0].tool_call_id == "d0"
        assert decision_results[1].tool_call_id == "d1"
        assert all(r.ok for r in decision_results)
        first_payload = json.loads(decision_results[0].result)
        second_payload = json.loads(decision_results[1].result)
        assert first_payload["decision"] == first_args["decision"]
        assert second_payload["decision"] == second_args["decision"]
        assert first_payload["decision_id"] != second_payload["decision_id"]
