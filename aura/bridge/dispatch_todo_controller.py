"""LEGACY — Compatibility alias for the canonical ExecutionChecklistController.

Production dispatch uses ``aura.execution_checklist.ExecutionChecklistController``
directly. This module exists only for older tests/import paths that still refer
to ``DispatchTodoController``. It is not the canonical TODO controller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aura.execution_checklist import ExecutionChecklistController

if TYPE_CHECKING:
    from aura.events import EventBus


class DispatchTodoController(ExecutionChecklistController):
    """Compatibility name for the Canonical Execution Checklist projector.

    .. deprecated::
        Use ``aura.execution_checklist.ExecutionChecklistController`` directly.
        This alias exists for older test/import compatibility only.
    """

    def __init__(self, event_bus: "EventBus | None" = None) -> None:
        super().__init__(event_bus=event_bus)

    def begin(
        self,
        tool_call_id: str,
        objectives: list[Any],
    ) -> list[dict[str, Any]]:
        return super().begin(tool_call_id, objectives)

    def activate_step(
        self,
        tool_call_id: str,
        step_id: str,
    ) -> list[dict[str, Any]] | None:
        return super().activate_step(tool_call_id, step_id)

    def complete_step(
        self,
        tool_call_id: str,
        step_id: str,
    ) -> list[dict[str, Any]] | None:
        return super().complete_step(tool_call_id, step_id)

    def finish(
        self,
        tool_call_id: str,
        *,
        ok: bool | None = None,
    ) -> list[dict[str, Any]] | None:
        return super().finish(tool_call_id, ok=ok)

    def snapshot(self, tool_call_id: str) -> list[dict[str, Any]]:
        return super().snapshot(tool_call_id)

    def has_active_tool_call(self, tool_call_id: str) -> bool:
        return self.has_active_campaign(tool_call_id)


__all__ = ["DispatchTodoController"]
