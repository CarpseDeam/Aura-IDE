from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from aura.client import Event
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.validation_orchestrator import looks_like_validation_command
from aura.conversation.worker_final_validation import run_explicit_validation_commands
from aura.conversation.worker_finalization_gate import handle_worker_candidate_finalization
from aura.conversation.worker_flow import (
    WORKER_FLOW_DOCS_VALIDATION_REQUIRED_TEXT,
    WORKER_FLOW_VALIDATION_REQUIRED_TEXT,
)
from aura.sandbox import WatchResult, classify_watch_outcome


def _state_with_changed_file(path: str) -> _SendState:
    state = _SendState(mode="worker", research_policy=None)
    assert state.worker_flow is not None
    state.worker_flow.observe_tool_result(
        "write_file",
        {"path": path},
        True,
        {"ok": True, "path": path, "applied": True},
    )
    return state


def _final_message() -> dict:
    return {
        "role": "assistant",
        "content": "Changed files: docs/gui-event-architecture.md\nValidation: python -m compileall docs/ passed.",
        "reasoning_content": None,
    }


def _finalize(
    state: _SendState,
    tmp_path: Path,
    *,
    explicit_validation_commands: list[str] | None = None,
    on_event: list[Event] | None = None,
) -> tuple[str, History, MagicMock]:
    history = History()
    finish = MagicMock()
    events = on_event if on_event is not None else []
    action = handle_worker_candidate_finalization(
        state=state,
        full_message=_final_message(),
        history=history,
        workspace_root=tmp_path,
        on_event=events.append,
        finish_worker_recoverable_followup=finish,
        handle_worker_flow_steering=lambda _state, _on_event: "none",
        explicit_validation_commands=explicit_validation_commands,
    )
    return action, history, finish


def test_docs_only_markdown_write_gets_docs_safe_validation_nudge(tmp_path: Path) -> None:
    state = _state_with_changed_file("docs/gui-event-architecture.md")
    state.syntax_validation_required.add("docs/gui-event-architecture.md")

    action, history, finish = _finalize(state, tmp_path)

    assert action == "continue"
    assert finish.call_count == 0
    assert state.syntax_validation_required == set()
    user_messages = [msg["content"] for msg in history.messages if msg.get("role") == "user"]
    assert user_messages == [WORKER_FLOW_DOCS_VALIDATION_REQUIRED_TEXT]
    assert "py_compile checks against markdown/text files" in user_messages[0]
    assert "smallest relevant py_compile or pytest" not in user_messages[0]


def test_python_source_write_keeps_python_validation_nudge(tmp_path: Path) -> None:
    state = _state_with_changed_file("aura/example.py")

    action, history, finish = _finalize(state, tmp_path)

    assert action == "continue"
    assert finish.call_count == 0
    user_messages = [msg["content"] for msg in history.messages if msg.get("role") == "user"]
    assert user_messages == [WORKER_FLOW_VALIDATION_REQUIRED_TEXT]
    assert "smallest relevant py_compile or pytest" in user_messages[0]


def test_docs_only_compileall_explicit_validation_satisfies_gate(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "gui-event-architecture.md").write_text("# GUI events\n", encoding="utf-8")
    state = _state_with_changed_file("docs/gui-event-architecture.md")
    events: list[Event] = []

    action, history, finish = _finalize(
        state,
        tmp_path,
        explicit_validation_commands=["python -m compileall docs/"],
        on_event=events,
    )

    assert action == "finished"
    assert finish.call_count == 0
    assert state.worker_flow is not None
    assert not state.worker_flow.requires_validation_before_final()
    assert any(msg.get("role") == "assistant" for msg in history.messages)
    assert any("compileall docs/" in str(getattr(event, "result", "")) for event in events)


def test_compileall_counts_as_validation_command() -> None:
    assert looks_like_validation_command("python -m compileall docs/")


def test_explicit_validation_uses_exit_zero_not_launch_traceback_heuristic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeSandbox:
        def __init__(self, **_kwargs) -> None:
            pass

        def run_and_watch(self, *_args, **_kwargs) -> WatchResult:
            return WatchResult(
                ok=False,
                survived_window=False,
                exited_early=True,
                error_detected=True,
                exit_code=0,
                output="Traceback (most recent call last):\nignored fallback\nok\n",
            )

    monkeypatch.setattr(
        "aura.conversation.worker_final_validation.SandboxExecutor",
        FakeSandbox,
    )

    result = run_explicit_validation_commands(
        workspace_root=tmp_path,
        commands=["python -m compileall docs/"],
    )

    assert result.ok is True
    assert result.runs is not None
    assert result.runs[0].ok is True


def test_launch_watch_remains_strict_for_traceback_output() -> None:
    result = classify_watch_outcome(
        still_running=False,
        exit_code=0,
        output="Traceback (most recent call last):\nboom\n",
        window_seconds=10,
    )

    assert result.ok is False
    assert result.error_detected is True
