"""Thin delegation layer extracted from ExecutionEventHandler.

Owns 9 one-line routing methods that forward bridge execution tool events to
AuraPlayground and ChatView. Keeps ExecutionEventHandler focused on lifecycle
orchestration rather than per-event forwarding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aura.task_checklist import UPDATE_TASK_CHECKLIST_TOOL

if TYPE_CHECKING:
    from aura.gui.chat_view import ChatView
    from aura.gui.playground import AuraPlayground


class ExecutionToolEventRouter:
    """Routes execution tool events to the appropriate UI component.

    Each method is a thin one-line forward — no logic, no state, no signals.
    """

    def __init__(self, playground: AuraPlayground, chat: ChatView) -> None:
        self._playground = playground
        self._chat = chat

    def on_execution_tool_call_start(
        self, tool_call_id: str, execution_tool_id: str, name: str
    ) -> None:
        """Forward tool call start to playground."""
        if name == UPDATE_TASK_CHECKLIST_TOOL:
            return
        self._playground.add_tool_call(execution_tool_id, name, parent_tool_id=tool_call_id)

    def on_execution_tool_args(
        self, tool_call_id: str, execution_tool_id: str, fragment: str
    ) -> None:
        """Forward tool call args delta to playground."""
        self._playground.append_tool_args(execution_tool_id, fragment)

    def on_execution_tool_result(
        self,
        parent_tool_id: str,
        execution_tool_id: str,
        name: str,
        ok: bool,
        result: str,
        extras: dict,
    ) -> None:
        """Forward tool result to playground."""
        if name == UPDATE_TASK_CHECKLIST_TOOL:
            return
        self._playground.set_tool_result(execution_tool_id, ok, result)

    def on_execution_diff_decided(
        self,
        parent_tool_id: str,
        execution_tool_id: str,
        decision: str,
        rel_path: str,
        old: str,
        new: str,
        is_new_file: bool,
    ) -> None:
        """Record the approval decision as a diff card.

        This is an approval-derived record for the user, not proof that the
        change reached disk -- the workspace editor itself is never driven
        from here. See ``on_execution_file_edit_lifecycle`` for the
        authoritative applied-content projection.
        """
        self._playground.add_diff_card(execution_tool_id, rel_path, old, new, decision, is_new_file)

    def on_execution_file_edit_lifecycle(
        self,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        phase: str,
        changes: list,
        reason: str,
    ) -> None:
        """Forward the authoritative file-edit lifecycle to the workspace editor."""
        self._playground.handle_file_edit_lifecycle(
            tool_call_id, tool_name, phase, changes, reason
        )

    def on_execution_terminal_output(
        self, parent_tool_id: str, execution_tool_id: str, text: str
    ) -> None:
        """Forward terminal output (execution mode) to playground."""
        self._playground.append_terminal_output(execution_tool_id, text)

    def on_execution_terminal_command_started(
        self,
        parent_tool_id: str,
        execution_tool_id: str,
        command: str,
        starting_cwd: str,
    ) -> None:
        """Open the terminal card only after real execution has started."""
        self._playground.start_terminal_command(
            execution_tool_id,
            command,
            starting_cwd,
        )

    def on_execution_agent_process_started(
        self, parent_tool_id: str, process_id: str, label: str, command: str
    ) -> None:
        """Forward CLI backend process start to playground terminal."""
        self._playground.start_terminal_process(process_id, command)

    def on_execution_agent_process_output(
        self, parent_tool_id: str, process_id: str, text: str
    ) -> None:
        """Forward CLI backend process output to playground terminal."""
        self._playground.append_terminal_output(process_id, text)

    def on_execution_agent_process_finished(
        self, parent_tool_id: str, process_id: str, exit_code: int
    ) -> None:
        """Forward CLI backend process completion to playground terminal."""
        self._playground.finish_terminal_process(process_id, exit_code)

    def on_terminal_output(self, tool_call_id: str, text: str) -> None:
        """Forward terminal output to the chat view."""
        self._chat.append_terminal_output(tool_call_id, text)
