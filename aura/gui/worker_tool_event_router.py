"""Thin delegation layer extracted from WorkerEventHandler.

Owns 9 one-line routing methods that forward bridge worker tool events to
AuraPlayground and ChatView. Keeps WorkerEventHandler focused on lifecycle
orchestration rather than per-event forwarding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aura.gui.chat_view import ChatView
    from aura.gui.playground import AuraPlayground


class WorkerToolEventRouter:
    """Routes worker tool events to the appropriate UI component.

    Each method is a thin one-line forward — no logic, no state, no signals.
    """

    def __init__(self, playground: AuraPlayground, chat: ChatView) -> None:
        self._playground = playground
        self._chat = chat

    def on_worker_tool_call_start(
        self, tool_call_id: str, worker_tool_id: str, name: str
    ) -> None:
        """Forward tool call start to playground."""
        self._playground.add_tool_call(worker_tool_id, name, parent_tool_id=tool_call_id)

    def on_worker_tool_args(
        self, tool_call_id: str, worker_tool_id: str, fragment: str
    ) -> None:
        """Forward tool call args delta to playground."""
        self._playground.append_tool_args(worker_tool_id, fragment)

    def on_worker_tool_result(
        self,
        parent_tool_id: str,
        worker_tool_id: str,
        name: str,
        ok: bool,
        result: str,
        extras: dict,
    ) -> None:
        """Forward tool result to playground."""
        self._playground.set_tool_result(worker_tool_id, ok, result)

    def on_worker_diff_decided(
        self,
        parent_tool_id: str,
        worker_tool_id: str,
        decision: str,
        rel_path: str,
        old: str,
        new: str,
        is_new_file: bool,
    ) -> None:
        """Forward diff decision to playground."""
        self._playground.show_code_diff(worker_tool_id, rel_path, old, new, decision)

    def on_worker_terminal_output(
        self, parent_tool_id: str, worker_tool_id: str, text: str
    ) -> None:
        """Forward terminal output (worker mode) to playground."""
        self._playground.append_terminal_output(worker_tool_id, text)

    def on_worker_agent_process_started(
        self, parent_tool_id: str, process_id: str, label: str, command: str
    ) -> None:
        """Forward CLI backend process start to playground terminal."""
        self._playground.start_terminal_process(process_id, command)

    def on_worker_agent_process_output(
        self, parent_tool_id: str, process_id: str, text: str
    ) -> None:
        """Forward CLI backend process output to playground terminal."""
        self._playground.append_terminal_output(process_id, text)

    def on_worker_agent_process_finished(
        self, parent_tool_id: str, process_id: str, exit_code: int
    ) -> None:
        """Forward CLI backend process completion to playground terminal."""
        self._playground.finish_terminal_process(process_id, exit_code)

    def on_terminal_output(self, tool_call_id: str, text: str) -> None:
        """Forward terminal output (single mode) to chat view."""
        self._chat.append_terminal_output(tool_call_id, text)
