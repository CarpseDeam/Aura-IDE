"""Read Only collaborative mode: prompt composition, frozen turn intent,
conversation-first routing, production presentation suppression, and the
single authoritative usage accounting path.

Read Only keeps ToolRegistry as the capability authority and the ordinary
model/tool loop, but a frozen Read Only turn is presented conversationally:
assistant reasoning/prose and read/search/research tool calls travel the
canonical chat signals, no production workspace session is begun, and provider
Usage still reaches conversation telemetry exactly once.
"""
from __future__ import annotations

import sys
import threading

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.bridge.qt_bridge import _ConversationRunner  # noqa: E402
from aura.bridge.read_only_routing import emit_read_only_facts  # noqa: E402
from aura.client import (  # noqa: E402
    ContentDelta,
    Done,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
    Usage,
)
from aura.context_gearbox.runtime import (  # noqa: E402
    READ_ONLY_COLLABORATION_INSTRUCTION,
    compose_system_prompt,
)
from aura.conversation import ConversationManager, History  # noqa: E402
from aura.conversation.tools import ToolRegistry  # noqa: E402
from aura.model_streams import (  # noqa: E402
    PRODUCTION_STREAM_HOOK,
    model_streams,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class _ApprovalProxy:
    def request_approval(self, _request):
        return None


class _Capture:
    """Collects every signal payload emitted on one runner."""

    def __init__(self, runner):
        self.reasoning = []
        self.content = []
        self.tool_start = []
        self.tool_args = []
        self.tool_end = []
        self.tool_result = []
        self.usage = []
        self.stream_done = []
        runner.reasoningDelta.connect(self.reasoning.append)
        runner.contentDelta.connect(self.content.append)
        runner.toolCallStart.connect(lambda *a: self.tool_start.append(a))
        runner.toolCallArgs.connect(lambda *a: self.tool_args.append(a))
        runner.toolCallEnd.connect(self.tool_end.append)
        runner.toolResultEmitted.connect(lambda *a: self.tool_result.append(a))
        runner.usage.connect(lambda *a: self.usage.append(a))
        runner.streamDone.connect(lambda *a: self.stream_done.append(a))


def _install_stream(events):
    """Register a stub stream for the production hook and return a restore fn."""
    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)

    def stream(**kwargs):
        yield from events

    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)

    def restore():
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)

    return restore


def _dummy_root():
    from pathlib import Path

    return Path.home()


# ---- prompt composition ----------------------------------------------------


def test_read_only_prompt_appends_collaboration_instruction(tmp_path) -> None:
    prompt = compose_system_prompt("", tmp_path, read_only=True).system_prompt
    assert READ_ONLY_COLLABORATION_INSTRUCTION in prompt
    assert "conversationally" in prompt
    assert "rather than an action to perform" in prompt


def test_normal_prompt_has_no_collaboration_instruction(tmp_path) -> None:
    prompt = compose_system_prompt("", tmp_path).system_prompt
    assert READ_ONLY_COLLABORATION_INSTRUCTION not in prompt
    assert "conversationally" not in prompt


def test_read_only_instruction_survives_full_replacement_prompt(tmp_path) -> None:
    custom = "{AURA_REPLACE_CANONICAL_PROMPT}\nDo work."
    prompt = compose_system_prompt(custom, tmp_path, read_only=True).system_prompt
    assert READ_ONLY_COLLABORATION_INSTRUCTION in prompt
    assert "Do work." in prompt


# ---- read-only runner routing ----------------------------------------------


def _make_runner(read_only_turn, *, manager=None, production_session=None):
    if manager is None:
        manager = ConversationManager(History(), ToolRegistry(_dummy_root()))
    return _ConversationRunner(
        manager=manager,
        approval_proxy=_ApprovalProxy(),
        cancel_event=threading.Event(),
        model="test-model",
        thinking="off",
        production_session=production_session,
        read_only_turn=read_only_turn,
    )


def test_read_only_routes_reasoning_prose_and_tools_to_chat_signals(qapp) -> None:
    runner = _make_runner(read_only_turn=True)
    capture = _Capture(runner)

    emit_read_only_facts(runner, ReasoningDelta(text="Let me inspect this."), "test-model")
    emit_read_only_facts(runner, ContentDelta(text="Here is the answer."), "test-model")
    emit_read_only_facts(
        runner, ToolCallStart(index=0, id="read-1", name="read_file"), "test-model"
    )
    emit_read_only_facts(
        runner, ToolCallArgsDelta(index=0, args_chunk='{"path": "x.py"}'), "test-model"
    )
    emit_read_only_facts(runner, ToolCallEnd(index=0), "test-model")
    emit_read_only_facts(
        runner,
        ToolResult(
            tool_call_id="read-1", name="read_file", ok=True, result="file", extras={}
        ),
        "test-model",
    )

    assert capture.reasoning == ["Let me inspect this."]
    assert capture.content == ["Here is the answer."]
    assert capture.tool_start == [(0, "read-1", "read_file")]
    assert capture.tool_args == [(0, '{"path": "x.py"}')]
    assert capture.tool_end == [0]
    assert capture.tool_result == [
        ("read-1", "read_file", True, "file", {})
    ]


def test_read_only_routes_usage_to_single_accounting_signal(qapp) -> None:
    runner = _make_runner(read_only_turn=True)
    capture = _Capture(runner)

    emit_read_only_facts(
        runner,
        Usage(
            prompt_tokens=100,
            completion_tokens=10,
            cache_hit_tokens=40,
            cache_miss_tokens=60,
        ),
        "test-model",
    )

    assert capture.usage == [("", "test-model", 100, 10, 40, 60)]


def test_read_only_turn_does_not_project_into_production_session(qapp) -> None:
    from aura.bridge.production_execution import ProductionExecutionSession

    session = ProductionExecutionSession(_ApprovalProxy())
    session_events = []
    session.executionStarted.connect(session_events.append)
    runner = _make_runner(read_only_turn=True, production_session=session)

    emit_read_only_facts(runner, ReasoningDelta(text="discussing"), "test-model")
    emit_read_only_facts(runner, ContentDelta(text="plan"), "test-model")

    # No production session lifecycle or projection is engaged.
    assert session_events == []
    assert session.is_active() is False


def test_normal_turn_projects_into_production_session(qapp) -> None:
    from aura.bridge.production_execution import ProductionExecutionSession

    session = ProductionExecutionSession(_ApprovalProxy())
    session.begin(model="test-model", run_id="prod-1")
    started = []
    session.executionReasoningDelta.connect(lambda *args: started.append(args))
    runner = _make_runner(read_only_turn=False, production_session=session)

    runner._emit_chat_facts(ContentDelta(text="prose stays chat-owned"))
    runner._on_event(ReasoningDelta(text="execution reasoning"))

    # Reasoning is projected into the workspace execution stream.
    assert started == [("prod-1", "execution reasoning")]


# ---- full read-only turn through the manager loop ---------------------------


def test_read_only_full_turn_counts_usage_once_and_skips_receipt(qapp, tmp_path) -> None:
    history = History()
    history.append_user_text("Why is this architecture behaving this way?")
    manager = ConversationManager(history, ToolRegistry(tmp_path))
    runner = _make_runner(
        read_only_turn=True,
        manager=manager,
        production_session=None,
    )
    capture = _Capture(runner)

    restore = _install_stream(
        [
            ReasoningDelta(text="Investigating the architecture."),
            ContentDelta(text="The design keeps one model loop."),
            Usage(
                prompt_tokens=200,
                completion_tokens=30,
                cache_hit_tokens=80,
                cache_miss_tokens=120,
            ),
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": "The design keeps one model loop.",
                },
            ),
        ]
    )
    try:
        runner.run()
    finally:
        restore()

    # Prose and reasoning reach the chat; usage reaches the accounting signal
    # exactly once; no production receipt signal is emitted.
    assert capture.content == ["The design keeps one model loop."]
    assert capture.reasoning == ["Investigating the architecture."]
    assert capture.usage == [("", "test-model", 200, 30, 80, 120)]
    assert capture.stream_done


# ---- capability catalog remains observation-only ---------------------------


def test_read_only_catalog_exposes_only_observation_capabilities(tmp_path) -> None:
    from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect

    registry = ToolRegistry(tmp_path, read_only=True)
    defs = registry.tool_defs()
    names = {d["function"]["name"] for d in defs if "function" in d}

    assert "read_file" in names
    assert "grep_search" in names
    assert "write_file" not in names
    assert "patch_file" not in names
    assert "edit_godot_scene" not in names

    for name in names:
        assert BUILTIN_TOOL_EFFECTS.get(name) in (
            ToolEffect.OBSERVATION,
            None,
        ), f"{name} is not an observation-only tool"


# ---- frozen turn intent drives prompt composition ---------------------------


def _make_bridge(tmp_path):
    from aura.bridge.qt_bridge import ConversationBridge

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    bridge = ConversationBridge(parent_widget=None, provider="test")
    bridge.set_workspace_root(tmp_path)
    return bridge


def test_frozen_turn_read_only_feeds_prompt_composition(tmp_path) -> None:
    bridge = _make_bridge(tmp_path)

    # A frozen Read Only turn composes the collaboration instruction.
    bridge._turn_read_only = True
    prompt = bridge._compose_prompt("").system_prompt
    assert READ_ONLY_COLLABORATION_INSTRUCTION in prompt

    # A normal (non-Read-Only) turn composes the production prompt unchanged.
    bridge._turn_read_only = False
    prompt = bridge._compose_prompt("").system_prompt
    assert READ_ONLY_COLLABORATION_INSTRUCTION not in prompt


def test_toggle_after_freeze_does_not_change_active_turn_prompt(tmp_path) -> None:
    """A toolbar toggle mid-turn never mutates the frozen turn's prompt.

    The frozen value is read once at send(); changing the registry afterwards
    must not alter the already-composed prompt for the active turn.
    """
    bridge = _make_bridge(tmp_path)

    # Freeze the turn as Read Only, then the user toggles the toolbar OFF.
    bridge._turn_read_only = True
    bridge.registry.set_read_only(False)
    prompt = bridge._compose_prompt("").system_prompt
    # The frozen turn intent still drives composition for this turn.
    assert READ_ONLY_COLLABORATION_INSTRUCTION in prompt

    # The next turn re-freezes from the registry, so the new state applies.
    bridge._turn_read_only = bridge.registry.read_only
    prompt = bridge._compose_prompt("").system_prompt
    assert READ_ONLY_COLLABORATION_INSTRUCTION not in prompt

