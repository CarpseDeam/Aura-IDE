from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from aura.client.events import ContentDelta, Done, Event, ToolCallStart, ToolResult
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools._types import ApprovalDecision, ToolExecResult
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.worker_final_validation import WorkerFinalValidationResult
from aura.hooks import hooks
from aura.sandbox import SandboxResult


def _done_with_tool(
    tool_call_id: str,
    name: str,
    args: dict,
    *,
    content: str = "",
) -> Done:
    return Done(
        finish_reason="tool_calls",
        full_message={
            "role": "assistant",
            "content": content,
            "reasoning_content": None,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }
            ],
        },
    )


def _tool_defs(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": name}} for name in names]


def _tool_names(tool_defs: list[dict]) -> set[str]:
    return {tool["function"]["name"] for tool in tool_defs}


@pytest.fixture
def worker_backend():
    backend = MagicMock()
    hooks.register("generate_worker_code", backend)
    try:
        yield backend
    finally:
        hooks.unregister("generate_worker_code")


@pytest.fixture
def worker_manager(tmp_path: Path) -> tuple[ConversationManager, MagicMock]:
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    tools.execute.return_value = ToolExecResult(ok=True, payload={"ok": True, "path": "aura/config.py"})
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    type(tools).mode = PropertyMock(return_value="worker")
    return ConversationManager(History(), tools), tools


def _approval_cb(_request=None) -> ApprovalDecision:
    return ApprovalDecision("approve")


def test_worker_terminal_source_inspection_is_not_executed(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, _tools = worker_manager
    events: list[Event] = []
    command = 'python -c "from pathlib import Path; print(Path(\'graph_main_window.py\').read_text())"'
    worker_backend.side_effect = [
        iter([_done_with_tool("term1", "run_terminal_command", {"command": command})]),
        iter([Done(finish_reason="stop", full_message={"role": "assistant", "content": "Done.", "reasoning_content": None})]),
    ]

    with patch("aura.conversation.tool_runner.SandboxExecutor") as sandbox_cls:
        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
        )

    sandbox_cls.assert_not_called()
    terminal_results = [
        event for event in events
        if isinstance(event, ToolResult) and event.name == "run_terminal_command"
    ]
    assert terminal_results
    payload = json.loads(terminal_results[-1].result)
    assert terminal_results[-1].ok is False
    assert payload["failure_class"] == "source_inspection_command_blocked"
    assert payload["suggested_next_tool"] == "read_file"
    assert payload["blocked_command"] == command
    assert not [
        event for event in events
        if isinstance(event, ToolCallStart) and event.name == "run_terminal_command"
    ]


def test_worker_tool_call_preamble_content_is_suppressed(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, _tools = worker_manager
    events: list[Event] = []
    preamble = "Let me read aura/config.py before I make the edit."
    final_summary = "Done."
    worker_backend.side_effect = [
        iter([
            ContentDelta(preamble),
            ToolCallStart(index=0, id="read1", name="read_file"),
            _done_with_tool(
                "read1",
                "read_file",
                {"path": "aura/config.py"},
                content=preamble,
            ),
        ]),
        iter([
            ContentDelta(final_summary),
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": final_summary,
                    "reasoning_content": None,
                },
            ),
        ]),
    ]

    manager.send(
        on_event=events.append,
        approval_cb=_approval_cb,
        cancel_event=threading.Event(),
        model="deepseek-chat",
        thinking="off",
        hook_name="generate_worker_code",
    )

    assert [event.text for event in events if isinstance(event, ContentDelta)] == [final_summary]
    assert [
        event.full_message.get("content") for event in events if isinstance(event, Done)
    ] == [final_summary]
    assert any(
        isinstance(event, ToolCallStart) and event.name == "read_file"
        for event in events
    )
    assert any(
        isinstance(event, ToolResult) and event.name == "read_file"
        for event in events
    )
    assistant_messages = [
        message.get("content")
        for message in manager.history.messages
        if message.get("role") == "assistant"
    ]
    assert assistant_messages == [preamble, final_summary]


def test_worker_py_compile_still_executes(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, _tools = worker_manager
    events: list[Event] = []
    worker_backend.side_effect = [
        iter([_done_with_tool("term1", "run_terminal_command", {"command": "python -m py_compile aura/config.py"})]),
        iter([Done(finish_reason="stop", full_message={"role": "assistant", "content": "Done.", "reasoning_content": None})]),
    ]

    with (
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
        patch("aura.conversation.tool_runner.SandboxExecutor") as sandbox_cls,
    ):
        load_settings.return_value = MagicMock(sandbox_mode="host")
        sandbox = MagicMock()
        sandbox.run_terminal_command.return_value = SandboxResult(
            ok=True,
            stdout="",
            stderr="",
            exit_code=0,
        )
        sandbox_cls.return_value = sandbox

        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
        )

    sandbox.run_terminal_command.assert_called_once()
    terminal_results = [
        event for event in events
        if isinstance(event, ToolResult) and event.name == "run_terminal_command"
    ]
    assert terminal_results[-1].ok is True


def test_worker_pytest_missing_from_project_env_reports_setup_needed(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, tools = worker_manager
    (tools.workspace_root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    events: list[Event] = []
    command = "pytest tests/test_x.py"
    worker_backend.side_effect = [
        iter([_done_with_tool("term1", "run_terminal_command", {"command": command})]),
        iter([Done(finish_reason="stop", full_message={"role": "assistant", "content": "Done.", "reasoning_content": None})]),
    ]

    with patch("aura.conversation.tool_runner.SandboxExecutor") as sandbox_cls:
        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
        )

    sandbox_cls.assert_not_called()
    terminal_results = [
        event for event in events
        if isinstance(event, ToolResult) and event.name == "run_terminal_command"
    ]
    assert terminal_results[-1].ok is False
    payload = json.loads(terminal_results[-1].result)
    assert payload["failure_class"] == "project_environment_missing_dependency"
    assert payload["missing_dependency"] == "pytest"
    assert payload["blocked_command"] == command


def test_worker_pytest_executes_through_project_venv_when_available(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, tools = worker_manager
    workspace_root = tools.workspace_root
    python = workspace_root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    events: list[Event] = []
    command = "pytest tests/test_x.py"
    worker_backend.side_effect = [
        iter([_done_with_tool("term1", "run_terminal_command", {"command": command})]),
        iter([Done(finish_reason="stop", full_message={"role": "assistant", "content": "Done.", "reasoning_content": None})]),
    ]

    with (
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
        patch("aura.python_env.project_module_available", return_value=True),
        patch("aura.conversation.tool_runner.SandboxExecutor") as sandbox_cls,
    ):
        load_settings.return_value = MagicMock(sandbox_mode="host")
        sandbox = MagicMock()
        sandbox.run_terminal_command.return_value = SandboxResult(
            ok=True,
            stdout="",
            stderr="",
            exit_code=0,
        )
        sandbox_cls.return_value = sandbox

        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
        )

    sandbox.run_terminal_command.assert_called_once()
    rewritten = sandbox.run_terminal_command.call_args.kwargs["command"]
    assert str(python) in rewritten
    assert "-m pytest tests/test_x.py" in rewritten


def test_worker_project_local_dependency_install_executes(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, tools = worker_manager
    workspace_root = tools.workspace_root
    python = workspace_root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    events: list[Event] = []
    command = r".venv\Scripts\python.exe -m pip install -e ."
    worker_backend.side_effect = [
        iter([_done_with_tool("term1", "run_terminal_command", {"command": command})]),
        iter([Done(finish_reason="stop", full_message={"role": "assistant", "content": "Done.", "reasoning_content": None})]),
    ]

    with (
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
        patch("aura.conversation.tool_runner.SandboxExecutor") as sandbox_cls,
    ):
        load_settings.return_value = MagicMock(sandbox_mode="host")
        sandbox = MagicMock()
        sandbox.run_terminal_command.return_value = SandboxResult(
            ok=True,
            stdout="installed\n",
            stderr="",
            exit_code=0,
        )
        sandbox_cls.return_value = sandbox

        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
        )

    sandbox.run_terminal_command.assert_called_once()
    rewritten = sandbox.run_terminal_command.call_args.kwargs["command"]
    assert str(python) in rewritten
    assert "-m pip install -e ." in rewritten
    terminal_results = [
        event for event in events
        if isinstance(event, ToolResult) and event.name == "run_terminal_command"
    ]
    assert terminal_results[-1].ok is True


def test_worker_explicit_validation_command_executes(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, _tools = worker_manager
    events: list[Event] = []
    command = "python tools/custom_validation.py --smoke"
    worker_backend.side_effect = [
        iter([_done_with_tool("term1", "run_terminal_command", {"command": command})]),
        iter([Done(finish_reason="stop", full_message={"role": "assistant", "content": "Done.", "reasoning_content": None})]),
    ]

    with (
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
        patch("aura.conversation.tool_runner.SandboxExecutor") as sandbox_cls,
        patch("aura.conversation.manager.run_explicit_validation_commands") as run_validation,
    ):
        load_settings.return_value = MagicMock(sandbox_mode="host")
        run_validation.return_value = WorkerFinalValidationResult(ok=True)
        sandbox = MagicMock()
        sandbox.run_terminal_command.return_value = SandboxResult(
            ok=True,
            stdout="ok",
            stderr="",
            exit_code=0,
        )
        sandbox_cls.return_value = sandbox

        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
            explicit_validation_commands=[command],
        )

    sandbox.run_terminal_command.assert_called_once()


def test_worker_final_text_is_quarantined_until_explicit_validation_passes(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, _tools = worker_manager
    events: list[Event] = []
    command = 'python -c "assert False"'
    worker_backend.side_effect = [
        iter([
            ContentDelta("All set before validation."),
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": "All set before validation.",
                    "reasoning_content": None,
                },
            ),
        ]),
        iter([
            ContentDelta("Fixed after validation."),
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": "Fixed after validation.",
                    "reasoning_content": None,
                },
            ),
        ]),
    ]

    with patch("aura.conversation.manager.run_explicit_validation_commands") as run_validation:
        run_validation.side_effect = [
            WorkerFinalValidationResult(
                ok=False,
                command=command,
                diagnostics="AssertionError",
            ),
            WorkerFinalValidationResult(ok=True),
        ]
        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
            explicit_validation_commands=[command],
        )

    content = [event.text for event in events if isinstance(event, ContentDelta)]
    assert content == ["Fixed after validation."]
    validation_results = [
        event for event in events
        if isinstance(event, ToolResult) and event.tool_call_id == "auto_explicit_validation"
    ]
    assert validation_results
    assert validation_results[0].ok is False
    assistant_messages = [
        message.get("content")
        for message in manager.history.messages
        if message.get("role") == "assistant"
    ]
    assert assistant_messages == ["Fixed after validation."]


def test_worker_does_not_retry_malformed_explicit_validation_command(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, _tools = worker_manager
    events: list[Event] = []
    command = "Run pytest and make sure it passes"
    worker_backend.side_effect = [
        iter([
            ContentDelta("Done after code change."),
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": "Done after code change.",
                    "reasoning_content": None,
                },
            ),
        ]),
    ]

    manager.send(
        on_event=events.append,
        approval_cb=_approval_cb,
        cancel_event=threading.Event(),
        model="deepseek-chat",
        thinking="off",
        hook_name="generate_worker_code",
        explicit_validation_commands=[command],
    )

    assert worker_backend.call_count == 1
    validation_results = [
        event for event in events
        if isinstance(event, ToolResult) and event.tool_call_id == "auto_explicit_validation"
    ]
    assert validation_results
    payload = json.loads(validation_results[0].result)
    assert payload["classification"] == "malformed_validation_command"
    assistant_messages = [
        message.get("content")
        for message in manager.history.messages
        if message.get("role") == "assistant"
    ]
    assert assistant_messages == ["Done after code change."]


def test_worker_repeated_product_validation_failure_stops_cleanly(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, _tools = worker_manager
    events: list[Event] = []
    command = 'python -c "assert False"'
    worker_backend.side_effect = [
        iter([
            ContentDelta("Done before validation."),
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": "Done before validation.",
                    "reasoning_content": None,
                },
            ),
        ]),
        iter([
            ContentDelta("Still done."),
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": "Still done.",
                    "reasoning_content": None,
                },
            ),
        ]),
    ]

    with patch("aura.conversation.manager.run_explicit_validation_commands") as run_validation:
        run_validation.side_effect = [
            WorkerFinalValidationResult(
                ok=False,
                command=command,
                diagnostics="AssertionError",
            ),
            WorkerFinalValidationResult(
                ok=False,
                command=command,
                diagnostics="AssertionError",
            ),
        ]
        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
            explicit_validation_commands=[command],
        )

    assert worker_backend.call_count == 2
    assistant_messages = [
        message.get("content")
        for message in manager.history.messages
        if message.get("role") == "assistant"
    ]
    assert len(assistant_messages) == 1
    payload = json.loads(assistant_messages[0])
    assert payload["failure_class"] == "product_validation_failed"
    assert "one focused repair attempt" in payload["error"]


def test_worker_structured_read_and_patch_file_are_unaffected(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, tools = worker_manager
    events: list[Event] = []
    tools.execute.side_effect = [
        ToolExecResult(ok=True, payload={"ok": True, "path": "docs/notes.md"}),
        ToolExecResult(ok=True, payload={"ok": True, "path": "docs/notes.md"}),
    ]
    worker_backend.side_effect = [
        iter([_done_with_tool("read1", "read_file", {"path": "docs/notes.md"})]),
        iter([_done_with_tool("edit1", "patch_file", {"path": "docs/notes.md", "edits": []})]),
        iter([_done_with_tool("term1", "run_terminal_command", {"command": "python -m py_compile aura/config.py"})]),
        iter([Done(finish_reason="stop", full_message={"role": "assistant", "content": "Done.", "reasoning_content": None})]),
    ]

    with (
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
        patch("aura.conversation.tool_runner.SandboxExecutor") as sandbox_cls,
    ):
        load_settings.return_value = MagicMock(sandbox_mode="host")
        sandbox = MagicMock()
        sandbox.run_terminal_command.return_value = SandboxResult(
            ok=True,
            stdout="",
            stderr="",
            exit_code=0,
        )
        sandbox_cls.return_value = sandbox

        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
        )

    executed_names = [
        call.args[0] if call.args else call.kwargs["name"]
        for call in tools.execute.call_args_list
    ]
    assert executed_names == ["read_file", "patch_file"]


def test_worker_flow_broad_ratchet_filters_and_blocks_broad_tools(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, tools = worker_manager
    tools.tool_defs.return_value = _tool_defs(
        "read_file",
        "grep_search",
        "read_file_outline",
        "read_file_range",
        "find_usages",
        "patch_file",
        "run_terminal_command",
    )
    events: list[Event] = []
    worker_backend.side_effect = [
        iter([
            _done_with_tool(
                "range1",
                "read_file_range",
                {"path": "aura/conversation/worker_flow.py", "start_line": 1, "end_line": 20},
                content=(
                    "I will edit aura/conversation/worker_flow.py and "
                    "tests/test_worker_flow.py, then run python -m pytest tests/test_worker_flow.py -q."
                ),
            ),
        ]),
        iter([
            _done_with_tool(
                "range2",
                "read_file_range",
                {"path": "aura/conversation/worker_flow.py", "start_line": 40, "end_line": 80},
                content=(
                    "Let me read the helper again. Now I have the full picture. "
                    "Let me plan the hunks."
                ),
            ),
        ]),
        iter([
            _done_with_tool("read1", "read_file", {"path": "aura/conversation/worker_flow.py"}),
        ]),
        iter([
            Done(
                finish_reason="stop",
                full_message={"role": "assistant", "content": "Done.", "reasoning_content": None},
            ),
        ]),
    ]

    manager.send(
        on_event=events.append,
        approval_cb=_approval_cb,
        cancel_event=threading.Event(),
        model="deepseek-chat",
        thinking="off",
        hook_name="generate_worker_code",
    )

    third_call_tools = worker_backend.call_args_list[2].kwargs["tools"]
    assert "read_file" not in _tool_names(third_call_tools)
    assert {
        "read_file_outline",
        "read_file_range",
        "find_usages",
        "patch_file",
        "run_terminal_command",
    }.issubset(_tool_names(third_call_tools))
    executed_names = [
        call.args[0] if call.args else call.kwargs["name"]
        for call in tools.execute.call_args_list
    ]
    assert executed_names == ["read_file_range", "read_file_range"]
    blocked_results = [
        event for event in events
        if isinstance(event, ToolResult) and event.name == "read_file"
    ]
    assert blocked_results
    payload = json.loads(blocked_results[-1].result)
    assert payload["failure_class"] == "worker_flow_broad_orientation_restricted"
    assert payload["recoverable"] is True


def test_worker_write_final_is_held_until_validation_runs(
    worker_manager: tuple[ConversationManager, MagicMock],
    worker_backend: MagicMock,
) -> None:
    manager, tools = worker_manager
    events: list[Event] = []
    tools.execute.return_value = ToolExecResult(
        ok=True,
        payload={"ok": True, "path": "docs/notes.md", "applied": True},
    )
    worker_backend.side_effect = [
        iter([_done_with_tool("write1", "write_file", {"path": "docs/notes.md", "content": "ok"})]),
        iter([
            ContentDelta("Done before validation."),
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": "Done before validation.",
                    "reasoning_content": None,
                },
            ),
        ]),
        iter([_done_with_tool("term1", "run_terminal_command", {"command": "python -m py_compile aura/config.py"})]),
        iter([
            ContentDelta("Done after validation."),
            Done(
                finish_reason="stop",
                full_message={
                    "role": "assistant",
                    "content": "Done after validation.",
                    "reasoning_content": None,
                },
            ),
        ]),
    ]

    with (
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
        patch("aura.conversation.tool_runner.SandboxExecutor") as sandbox_cls,
    ):
        load_settings.return_value = MagicMock(sandbox_mode="host")
        sandbox = MagicMock()
        sandbox.run_terminal_command.return_value = SandboxResult(
            ok=True,
            stdout="",
            stderr="",
            exit_code=0,
        )
        sandbox_cls.return_value = sandbox

        manager.send(
            on_event=events.append,
            approval_cb=_approval_cb,
            cancel_event=threading.Event(),
            model="deepseek-chat",
            thinking="off",
            hook_name="generate_worker_code",
        )

    assert [event.text for event in events if isinstance(event, ContentDelta)] == [
        "Done after validation."
    ]
    assistant_messages = [
        message.get("content")
        for message in manager.history.messages
        if message.get("role") == "assistant"
    ]
    assert "Done before validation." not in assistant_messages
    assert "Done after validation." in assistant_messages
    user_messages = [
        message.get("content")
        for message in manager.history.messages
        if message.get("role") == "user"
    ]
    assert any(
        "Worker Flow: files were changed and validation has not run yet" in str(message)
        for message in user_messages
    )


def test_normal_worker_low_level_edit_tools_stay_hidden(tmp_workspace: Path) -> None:
    names = {
        tool["function"]["name"]
        for tool in ToolRegistry(tmp_workspace, mode="worker").tool_defs()
    }

    assert "patch_file" in names
    assert "write_file" in names
    assert "apply_edit_transaction" not in names
    assert "edit_file" not in names
    assert "edit_symbol" not in names
    assert "edit_line_range" not in names


# ── Worker finish helper tests ──────────────────────────────────────────────


def test_build_worker_unrecoverable_message():
    from aura.conversation.worker_finish import build_worker_unrecoverable_message

    content, full_message = build_worker_unrecoverable_message(
        failure_class="test_failure",
        error="Something broke.",
    )
    payload = json.loads(content)
    assert payload == {"ok": False, "failure_class": "test_failure", "error": "Something broke."}
    assert full_message["role"] == "assistant"
    assert full_message["content"] == content
    assert full_message["reasoning_content"] is None


def test_build_worker_unrecoverable_message_with_details():
    from aura.conversation.worker_finish import build_worker_unrecoverable_message

    content, full_message = build_worker_unrecoverable_message(
        failure_class="test_failure",
        error="Something broke.",
        details={"reason": "unknown"},
    )
    payload = json.loads(content)
    assert payload == {
        "ok": False,
        "failure_class": "test_failure",
        "error": "Something broke.",
        "details": {"reason": "unknown"},
    }


def test_build_worker_recoverable_followup_message():
    from aura.conversation.worker_finish import build_worker_recoverable_followup_message

    content, full_message = build_worker_recoverable_followup_message(
        failure_class="test_failure",
        error="Need another pass.",
    )
    payload = json.loads(content)
    assert payload == {
        "ok": False,
        "recoverable": True,
        "needs_follow_up": True,
        "failure_class": "test_failure",
        "error": "Need another pass.",
    }
    assert full_message["role"] == "assistant"
    assert full_message["content"] == content
    assert full_message["reasoning_content"] is None


def test_build_worker_recoverable_followup_message_with_details():
    from aura.conversation.worker_finish import build_worker_recoverable_followup_message

    content, full_message = build_worker_recoverable_followup_message(
        failure_class="test_failure",
        error="Need another pass.",
        details={"attempts": 3},
    )
    payload = json.loads(content)
    assert payload == {
        "ok": False,
        "recoverable": True,
        "needs_follow_up": True,
        "failure_class": "test_failure",
        "error": "Need another pass.",
        "details": {"attempts": 3},
    }


# ── Worker patch_file state policy tests ─────────────────────────────────────


def test_patch_file_state_block_non_existing_path(tmp_path: Path) -> None:
    """Non-existing worker path returns None."""
    from aura.conversation.worker_patch_state_policy import patch_file_state_block

    result = patch_file_state_block(
        tool_call_id="t1",
        name="patch_file",
        path="nonexistent/file.py",
        args={},
        worker_file_state={},
        workspace_root=tmp_path,
    )
    assert result is None


def test_patch_file_state_block_missing_expected_hash(tmp_path: Path) -> None:
    """Existing file with missing expected_file_hash returns
    patch_file_missing_expected_hash."""
    from aura.conversation.worker_patch_state_policy import patch_file_state_block

    (tmp_path / "existing.py").write_text("ok", encoding="utf-8")

    result = patch_file_state_block(
        tool_call_id="t1",
        name="patch_file",
        path="existing.py",
        args={},
        worker_file_state={},
        workspace_root=tmp_path,
    )
    assert result is not None
    payload = json.loads(result["result_payload"])
    assert payload["failure_class"] == "patch_file_missing_expected_hash"
    assert payload["applied"] is False
    assert payload["write_outcome"] == "not_applied_edit_mechanics_blocked"


def test_patch_file_state_block_hash_mismatch(tmp_path: Path) -> None:
    """Existing file with stale/mismatched hash returns
    patch_file_hash_mismatch and includes latest_read_content_hash."""
    from aura.conversation.worker_patch_state_policy import patch_file_state_block

    (tmp_path / "existing.py").write_text("ok", encoding="utf-8")
    worker_file_state = {
        "existing.py": {
            "content_hash": "abc123",
            "fresh_for_patch": True,
            "last_read_tool": "read_file",
        }
    }

    result = patch_file_state_block(
        tool_call_id="t1",
        name="patch_file",
        path="existing.py",
        args={"expected_file_hash": "wrong_hash"},
        worker_file_state=worker_file_state,
        workspace_root=tmp_path,
    )
    assert result is not None
    payload = json.loads(result["result_payload"])
    assert payload["failure_class"] == "patch_file_hash_mismatch"
    assert payload["applied"] is False
    assert payload["write_outcome"] == "not_applied_edit_mechanics_blocked"
    assert payload["recoverable"] is True
    assert payload["stale"] is True
    assert payload["expected_file_hash"] == "wrong_hash"
    assert payload["latest_read_content_hash"] == "abc123"
    assert payload["last_read_tool"] == "read_file"


def test_patch_file_state_block_not_fresh_for_patch(tmp_path: Path) -> None:
    """Existing file with matching hash but not fresh_for_patch still
    returns patch_file_hash_mismatch."""
    from aura.conversation.worker_patch_state_policy import patch_file_state_block

    (tmp_path / "existing.py").write_text("ok", encoding="utf-8")
    worker_file_state = {
        "existing.py": {
            "content_hash": "abc123",
            "fresh_for_patch": False,
            "last_read_tool": "read_file",
        }
    }

    result = patch_file_state_block(
        tool_call_id="t1",
        name="patch_file",
        path="existing.py",
        args={"expected_file_hash": "abc123"},
        worker_file_state=worker_file_state,
        workspace_root=tmp_path,
    )
    assert result is not None
    payload = json.loads(result["result_payload"])
    assert payload["failure_class"] == "patch_file_hash_mismatch"


def test_patch_file_state_block_fresh_hash(tmp_path: Path) -> None:
    """Existing file with matching fresh hash returns None (allowed)."""
    from aura.conversation.worker_patch_state_policy import patch_file_state_block

    (tmp_path / "existing.py").write_text("ok", encoding="utf-8")
    worker_file_state = {
        "existing.py": {
            "content_hash": "abc123",
            "fresh_for_patch": True,
            "last_read_tool": "read_file",
        }
    }

    result = patch_file_state_block(
        tool_call_id="t1",
        name="patch_file",
        path="existing.py",
        args={"expected_file_hash": "abc123"},
        worker_file_state=worker_file_state,
        workspace_root=tmp_path,
    )
    assert result is None
