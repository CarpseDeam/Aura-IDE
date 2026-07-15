from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from aura.bridge.qt_bridge import ConversationBridge, _Worker
from aura.client import Done
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import SINGLE_SYSTEM_PROMPT, compose_system_prompt
from aura.conversation.execution_mode import INTERACTIVE_MODE, PLANNER_WORKER_MODE
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.persistence import load_conversation, save_conversation
from aura.conversation.tools import ToolRegistry
from aura.conversation.tools._types import ApprovalDecision
from aura.gui.conv_persistence import ConversationPersistence
from aura.gui.left_pane import LeftPane
from aura.gui.main_window import MainWindow
from aura.gui.main_window_toolbar import MainWindowToolbar
from aura.settings import AppSettings
from aura.model_streams import model_streams

_APP = QApplication.instance() or QApplication([])


def _app() -> QApplication:
    return _APP


def _tool_names(registry: ToolRegistry) -> set[str]:
    return {
        str(tool.get("function", {}).get("name"))
        for tool in registry.tool_defs()
    }


def test_bridge_defaults_to_planner_worker_and_switches_existing_objects(tmp_path: Path) -> None:
    _app()
    bridge = ConversationBridge(parent_widget=None)
    bridge.set_workspace_root(tmp_path)
    identities = (id(bridge), id(bridge.history), id(bridge.registry), id(bridge._manager))
    bridge.history.append_user_text("Keep this conversation")

    assert bridge.execution_mode == PLANNER_WORKER_MODE
    assert bridge.registry.mode == "planner"
    assert bridge.set_execution_mode(INTERACTIVE_MODE)
    assert bridge.execution_mode == INTERACTIVE_MODE
    assert bridge.registry.mode == "single"
    assert bridge.history.messages == [{"role": "user", "content": "Keep this conversation"}]
    assert identities == (id(bridge), id(bridge.history), id(bridge.registry), id(bridge._manager))
    assert bridge.set_execution_mode(PLANNER_WORKER_MODE)
    assert bridge.registry.mode == "planner"


def test_bridge_rejects_mode_change_while_running(tmp_path: Path) -> None:
    _app()
    bridge = ConversationBridge(parent_widget=None)
    bridge.set_workspace_root(tmp_path)
    bridge.is_running = lambda: True  # type: ignore[method-assign]

    assert not bridge.set_execution_mode(INTERACTIVE_MODE)
    assert bridge.execution_mode == PLANNER_WORKER_MODE
    assert bridge.registry.mode == "planner"


def test_interactive_turn_uses_primary_loop_without_dispatch_and_preserves_worker_settings(
    tmp_path: Path,
) -> None:
    _app()
    bridge = ConversationBridge(parent_widget=None)
    bridge.set_workspace_root(tmp_path)
    bridge.set_worker_model("preserved-worker-model")
    bridge.set_worker_thinking("max")
    bridge.set_worker_temperature(0.2)
    preserved = (
        bridge._dispatch_proxy._worker_model,
        bridge._dispatch_proxy._worker_thinking,
        bridge._dispatch_proxy._worker_temperature,
    )
    bridge.set_execution_mode(INTERACTIVE_MODE)

    class Manager:
        history = bridge.history

        def send(self, **kwargs):
            self.kwargs = kwargs

    manager = Manager()
    worker = _Worker(
        manager=manager,  # type: ignore[arg-type]
        approval_proxy=bridge._approval_proxy,
        dispatch_proxy=None,
        cancel_event=threading.Event(),
        model="primary-model",
        thinking="high",
    )
    worker.run()

    assert manager.kwargs["model"] == "primary-model"
    assert manager.kwargs["hook_name"] == "generate_planner_code"
    assert manager.kwargs["dispatch_cb"] is None
    assert manager.kwargs["workflow_state_cb"] is None
    assert preserved == (
        bridge._dispatch_proxy._worker_model,
        bridge._dispatch_proxy._worker_thinking,
        bridge._dispatch_proxy._worker_temperature,
    )


def test_model_configuration_owns_execution_selector_and_presentation() -> None:
    _app()
    pane = LeftPane(None)
    toolbar = MainWindowToolbar(AppSettings())

    assert pane._execution_mode_combo.parent() is pane._model_config_footer
    assert not hasattr(toolbar, "_execution_mode_combo")
    assert not hasattr(toolbar, "_interactive_badge")
    assert pane.execution_mode() == PLANNER_WORKER_MODE
    assert pane._planner_model_label.text() == "Planner:"
    assert not pane._worker_model_combo.isHidden()
    assert not pane._worker_thinking_combo.isHidden()
    pane._worker_model_combo.addItem("Preserved worker", "preserved-worker")
    pane.set_worker_model("preserved-worker")
    pane.set_worker_thinking("max")

    pane.set_planner_worker_mode(False)

    assert pane.execution_mode() == INTERACTIVE_MODE
    assert pane._planner_model_label.text() == "Model:"
    assert pane._planner_thinking_label.text() == "Thinking:"
    assert pane._worker_model_combo.isHidden()
    assert pane._worker_thinking_combo.isHidden()
    assert pane.current_worker_model() == "preserved-worker"
    assert pane.current_worker_thinking() == "max"

    pane.set_planner_worker_mode(True)

    assert pane.execution_mode() == PLANNER_WORKER_MODE
    assert pane._planner_model_label.text() == "Planner:"
    assert pane._planner_thinking_label.text() == "Thinking:"
    assert not pane._worker_model_combo.isHidden()
    assert not pane._worker_thinking_combo.isHidden()
    assert pane.current_worker_model() == "preserved-worker"
    assert pane.current_worker_thinking() == "max"


def test_mode_selector_running_state_and_dispatch_value_are_presentation_only() -> None:
    _app()
    pane = LeftPane(None)
    toolbar = MainWindowToolbar(AppSettings())
    toolbar.set_auto_dispatch(True)

    pane.set_response_running(True)
    assert not pane._execution_mode_combo.isEnabled()
    pane.set_response_running(False)
    assert pane._execution_mode_combo.isEnabled()

    toolbar.set_dispatch_available(False)
    assert not toolbar._auto_dispatch_switch.isEnabled()
    assert toolbar._auto_dispatch_switch.isChecked()

    toolbar.set_dispatch_available(True)
    assert toolbar._auto_dispatch_switch.isEnabled()
    assert toolbar._auto_dispatch_switch.isChecked()


def test_interactive_inventory_has_direct_mutation_and_dynamic_tools(tmp_path: Path) -> None:
    tools_dir = tmp_path / ".aura" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "workspace_tool.py").write_text(
        'def workspace_dynamic_local(value: str) -> dict:\n'
        '    """Workspace dynamic tool."""\n'
        '    return {"value": value}\n',
        encoding="utf-8",
    )
    single = ToolRegistry(tmp_path, mode="single")
    planner = ToolRegistry(tmp_path, mode="planner")
    single_names = _tool_names(single)
    planner_names = _tool_names(planner)

    assert "edit_godot_asset_preview" in single_names
    assert "workspace_dynamic_local" in single_names
    assert "dispatch_to_worker" not in single_names
    assert "dispatch_to_worker" in planner_names
    assert "edit_godot_asset_preview" not in planner_names
    assert "workspace_dynamic_local" not in planner_names


def test_interactive_prompt_states_direct_iterative_contract() -> None:
    prompt = SINGLE_SYSTEM_PROMPT
    assert "one direct, persistent conversational agent" in prompt
    assert "Do not dispatch to another agent" in prompt
    assert "Inspect the current artifact before changing it" in prompt
    assert "Read the user's requested place literally" in prompt
    assert "Never save a Godot scene unless explicitly requested" in prompt


def test_interactive_prompt_requires_result_driven_progressive_godot_rounds() -> None:
    prompt = SINGLE_SYSTEM_PROMPT.lower()
    assert "use `build_live_ruin` as the primary mutation tool" in prompt
    assert "apply the first safe, cohesive step immediately" in prompt
    assert "exactly one cohesive semantic operation in each `build_live_ruin` call" in prompt
    assert "wait for its result before choosing the next operation" in prompt
    assert "do not pre-submit future operations" in prompt
    assert "inside the same interactive turn" in prompt
    assert "after an interruption, inspect the reconstructable live semantic state" in prompt
    assert "without repeating its handle" in prompt


def test_interactive_prompt_builds_literal_connected_architecture() -> None:
    prompt = SINGLE_SYSTEM_PROMPT.lower()
    assert "build recognizable connected architecture" in prompt
    assert "start with the defining feature of the request" in prompt
    assert "gate passage, hall, courtyard, tower, room, or wall" in prompt
    assert "add floors, walls, openings, upper levels, ceilings, and stairs" in prompt
    assert "choose the next component from the user's request and the structure already present" in prompt
    assert "handles, spaces, levels, walls, openings, connections, piece count" in prompt
    assert "add_tower" not in prompt


def test_interactive_prompt_does_not_invent_joining_structures() -> None:
    prompt = SINGLE_SYSTEM_PROMPT.lower()
    assert "do not add an elevated bridge, span, connector, upper chamber" in prompt
    assert "unless the user explicitly requested it or it is plainly necessary for physical access" in prompt
    assert "use `add_supported_span` only for an explicitly requested elevated bridge" in prompt
    for removed in ["mass map", "vertical profiles", "styling affordances", "structural continuation candidates"]:
        assert removed not in prompt


def test_interactive_prompt_requires_exact_wall_assets_and_factual_visual_claims() -> None:
    prompt = SINGLE_SYSTEM_PROMPT.lower()
    assert "inspect the real wall-placeable catalog and select the exact `asset_id`" in prompt
    assert "never provide raw transforms or embed values" in prompt
    assert "successful semantic operation proves only the returned construction and validation facts" in prompt
    assert "without visual findings, let the user judge appearance" in prompt
    assert "mandatory vision" in prompt


def test_interactive_tool_loop_returns_each_live_step_before_model_selects_next(
    tmp_path: Path,
) -> None:
    tools_dir = tmp_path / ".aura" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "progressive_build.py").write_text(
        "from pathlib import Path\n"
        "import json\n"
        "def build_live_ruin(operations: list[dict]) -> dict:\n"
        "    state_path = Path('.aura/progressive_state.json')\n"
        "    state = json.loads(state_path.read_text()) if state_path.exists() else {'steps': [], 'handles': []}\n"
        "    operation = operations[0]\n"
        "    handle = operation.get('name', operation.get('run', ''))\n"
        "    if operation.get('name') and handle in state['handles']:\n"
        "        return {'ok': False, 'applied': False, 'error': 'duplicate handle'}\n"
        "    if operation.get('name'):\n"
        "        state['handles'].append(handle)\n"
        "    state['steps'].append(operation['operation'])\n"
        "    state_path.write_text(json.dumps(state))\n"
        "    count = [15, 26, 26][len(state['steps']) - 1]\n"
        "    return {'ok': True, 'applied': True, 'piece_count': count, 'piece_count_delta': [15, 11, 0][len(state['steps']) - 1], 'created_handles': [handle] if operation.get('name') else [], 'connections_added': [{'a': 'court', 'b': 'wing'}] if operation['operation'] == 'attach_room' else [], 'openings_added': [{'reference': 'wing_north:0'}] if operation['operation'] == 'insert_opening' else []}\n",
        encoding="utf-8",
    )
    history = History(messages=[{"role": "user", "content": "Build a court, dependent wing, and window."}])
    manager = ConversationManager(history, ToolRegistry(tmp_path, mode="single"))
    model_rounds: list[list[dict]] = []

    def stream(**kwargs):
        messages = kwargs["messages"]
        model_rounds.append(messages)
        round_index = len(model_rounds)
        if round_index == 1:
            operation = {"operation": "create_enclosure", "name": "court"}
        elif round_index == 2:
            previous = json.loads(messages[-1]["content"])
            previous = previous.get("result", previous)
            assert previous["piece_count"] == 15
            assert previous["created_handles"] == ["court"]
            operation = {"operation": "attach_room", "name": "wing", "wall": "court_east"}
        elif round_index == 3:
            previous = json.loads(messages[-1]["content"])
            previous = previous.get("result", previous)
            assert previous["piece_count"] == 26
            assert previous["connections_added"] == [{"a": "court", "b": "wing"}]
            operation = {"operation": "insert_opening", "run": "wing_north", "slot": 0}
        else:
            previous = json.loads(messages[-1]["content"])
            previous = previous.get("result", previous)
            assert previous["piece_count"] == 26
            assert previous["openings_added"] == [{"reference": "wing_north:0"}]
            yield Done(
                finish_reason="stop",
                full_message={"role": "assistant", "content": "Construction complete.", "reasoning_content": ""},
            )
            return
        tool_call = {
            "id": f"live-step-{round_index}",
            "type": "function",
            "function": {"name": "build_live_ruin", "arguments": json.dumps({"operations": [operation]})},
        }
        yield Done(
            finish_reason="tool_calls",
            full_message={
                "role": "assistant", "content": None, "reasoning_content": "",
                "tool_calls": [tool_call],
            },
        )

    hook = "_test_progressive_interactive_godot"
    model_streams.register(hook, stream)
    try:
        manager.send(
            on_event=lambda _event: None,
            approval_cb=lambda _request: ApprovalDecision(action="approve"),
            cancel_event=threading.Event(),
            model="test-model",
            thinking="off",
            hook_name=hook,
            max_tool_rounds=8,
        )
    finally:
        model_streams.unregister(hook)

    assert len(model_rounds) == 4
    assistant_calls = [
        message for message in history.messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert [len(message["tool_calls"]) for message in assistant_calls] == [1, 1, 1]
    assert [message["role"] for message in history.messages] == [
        "user", "assistant", "tool", "assistant", "tool", "assistant", "tool", "assistant",
    ]
    state = json.loads((tmp_path / ".aura/progressive_state.json").read_text(encoding="utf-8"))
    assert state == {
        "steps": ["create_enclosure", "attach_room", "insert_opening"],
        "handles": ["court", "wing"],
    }


def test_interactive_context_uses_latest_request_for_authored_skill(tmp_path: Path) -> None:
    skill_src = (
        Path(__file__).parents[1]
        / "scripts/personal/godot_knowledge/skills/godot_aura_workflow/SKILL.md"
    )
    skill_dst = tmp_path / ".aura/skills/authored/godot_aura_workflow/SKILL.md"
    skill_dst.parent.mkdir(parents=True)
    shutil.copy2(skill_src, skill_dst)
    request = "Build a ruined gatehouse beneath AuraPreview, capture it, and revise its visual composition."

    composed = compose_system_prompt(
        RuntimeRole.SINGLE,
        None,
        tmp_path,
        content=request,
    )

    assert "Godot Live Building — Procedural Co-Building" in composed.system_prompt
    assert "capture_godot_asset_preview" in composed.system_prompt
    assert "critique_godot_preview_local" in composed.system_prompt


def test_persistence_round_trips_interactive_and_missing_mode_defaults_planner(tmp_path: Path) -> None:
    _app()
    bridge = ConversationBridge(parent_widget=None)
    bridge.set_workspace_root(tmp_path)
    bridge.set_execution_mode(INTERACTIVE_MODE)
    bridge.history.append_user_text("first")
    bridge.history.append_user_text("correction")
    path = save_conversation(
        bridge.history,
        tmp_path,
        model="primary-model",
        thinking="high",
        planner_worker_mode=bridge.planner_worker_mode,
        planner_model="primary-model",
        worker_model="preserved-worker-model",
        planner_provider="deepseek",
        worker_provider="openrouter",
    )

    loaded = load_conversation(path)
    assert loaded.planner_worker_mode is False
    assert [message["content"] for message in loaded.history.messages] == ["first", "correction"]
    assert loaded.planner_model == "primary-model"
    assert loaded.worker_model == "preserved-worker-model"
    assert loaded.worker_provider == "openrouter"

    missing = tmp_path / "missing-mode.json"
    missing.write_text(
        json.dumps({"version": 2, "model": "primary-model", "thinking": "off"}),
        encoding="utf-8",
    )
    assert load_conversation(missing).planner_worker_mode is True


def test_idle_mode_change_persists_without_an_assistant_turn(
    tmp_path: Path, monkeypatch
) -> None:
    _app()
    bridge = ConversationBridge(parent_widget=None)
    bridge.set_workspace_root(tmp_path)
    bridge.history.append_user_text("Keep the current gatehouse work")
    bridge.set_worker_model("preserved-worker-model")
    bridge.set_worker_thinking("max")

    chat_items = [{"kind": "user", "text": "Keep the current gatehouse work"}]
    saved_path = save_conversation(
        bridge.history,
        tmp_path,
        model="primary-model",
        thinking="high",
        planner_worker_mode=True,
        planner_model="primary-model",
        worker_model="preserved-worker-model",
        worker_thinking="max",
        chat_items=chat_items,
        planner_provider="deepseek",
        worker_provider="openrouter",
    )

    class Chat:
        def __init__(self, items):
            self.chat_items = items

        def add_error(self, _title, _message):
            raise AssertionError("mode persistence unexpectedly failed")

    persistence = ConversationPersistence(
        bridge=bridge,
        chat=Chat(chat_items),
        playground=object(),
        input_panel=object(),
        left_pane=object(),
        settings=SimpleNamespace(),
    )
    persistence._current_conversation_path = saved_path
    monkeypatch.setattr(persistence, "_update_project_thread", lambda *_args: None)

    window = SimpleNamespace(
        _bridge=bridge,
        _persistence=persistence,
        _workspace_root=tmp_path,
        _settings=SimpleNamespace(
            planner_system_prompt="",
            system_prompt="",
            provider="deepseek",
            planner_provider="deepseek",
            worker_provider="openrouter",
        ),
        current_model=lambda: "primary-model",
        current_thinking=lambda: "high",
        current_worker_model=lambda: "preserved-worker-model",
        current_worker_thinking=lambda: "max",
        _sync_execution_mode_ui=lambda _enabled: None,
    )
    window._apply_planner_worker_mode_to_bridge = MethodType(
        MainWindow._apply_planner_worker_mode_to_bridge, window
    )
    window._auto_save_current_conversation = MethodType(
        MainWindow._auto_save_current_conversation, window
    )

    MainWindow._on_execution_mode_changed(window, INTERACTIVE_MODE)

    reloaded = load_conversation(saved_path)
    assert reloaded.planner_worker_mode is False
    assert reloaded.history.messages == [
        {"role": "user", "content": "Keep the current gatehouse work"}
    ]
    assert reloaded.chat_items == chat_items
    assert reloaded.planner_model == "primary-model"
    assert reloaded.worker_model == "preserved-worker-model"
    assert reloaded.worker_thinking == "max"
    assert reloaded.worker_provider == "openrouter"


def test_rejected_or_unchanged_mode_change_does_not_autosave(monkeypatch) -> None:
    saves: list[bool] = []
    notices: list[str] = []

    class Bridge:
        planner_worker_mode = True

        def __init__(self, running: bool):
            self.running = running

        def is_running(self) -> bool:
            return self.running

    window = SimpleNamespace(
        _bridge=Bridge(running=True),
        _sync_execution_mode_ui=lambda _enabled: None,
        _apply_planner_worker_mode_to_bridge=lambda _enabled: True,
        _auto_save_current_conversation=lambda **_kwargs: saves.append(True),
    )
    monkeypatch.setattr(
        "aura.gui.main_window.QMessageBox.information",
        lambda *_args: notices.append("running"),
    )

    MainWindow._on_execution_mode_changed(window, INTERACTIVE_MODE)
    window._bridge.running = False
    MainWindow._on_execution_mode_changed(window, PLANNER_WORKER_MODE)

    assert notices == ["running"]
    assert saves == []


def test_new_chat_returns_existing_bridge_to_planner_worker(tmp_path: Path) -> None:
    _app()
    bridge = ConversationBridge(parent_widget=None)
    bridge.set_workspace_root(tmp_path)
    bridge.set_execution_mode(INTERACTIVE_MODE)
    bridge.history.append_user_text("existing work")

    class Chat:
        def reset(self):
            self.reset_called = True

    class Playground:
        def clear(self):
            self.clear_called = True

    persistence = ConversationPersistence(
        bridge=bridge,
        chat=Chat(),
        playground=Playground(),
        input_panel=object(),
        left_pane=object(),
        settings=SimpleNamespace(),
    )
    restored_modes: list[bool] = []
    persistence.execution_mode_restored.connect(restored_modes.append)

    persistence.new_conversation()

    assert bridge.execution_mode == PLANNER_WORKER_MODE
    assert bridge.registry.mode == "planner"
    assert bridge.history.messages == []
    assert restored_modes == [True]


def test_interactive_mode_does_not_add_deep_runtime_branches() -> None:
    root = Path(__file__).parents[1] / "aura/conversation"
    for name in ("manager.py", "manager_tool_round.py", "dispatch.py"):
        assert "interactive" not in (root / name).read_text(encoding="utf-8").lower()
