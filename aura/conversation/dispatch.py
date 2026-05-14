"""Planner -> Worker dispatch types.

The planner manager calls `dispatch_to_worker` (a tool) when it has enough
information to delegate a code change. Args are validated here, the manager
emits a WorkerDispatchRequested event to the GUI, then calls a
DispatchCallback to actually run the worker; the result is fed back to the
planner as the tool_result for that call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class WorkerDispatchRequest:
    goal: str
    files: list[str]
    spec: str
    acceptance: str
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "files": list(self.files),
            "spec": self.spec,
            "acceptance": self.acceptance,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerDispatchRequest":
        files = data.get("files") or []
        if not isinstance(files, list):
            files = []
        return cls(
            goal=str(data.get("goal", "")),
            files=[str(f) for f in files],
            spec=str(data.get("spec", "")),
            acceptance=str(data.get("acceptance", "")),
            summary=str(data.get("summary", "")),
        )


@dataclass
class WorkerDispatchResult:
    ok: bool
    summary: str
    cancelled: bool = False
    needs_followup: bool = False
    phase_boundary: bool = False
    followup_reason: str | None = None
    recoverable: bool = False
    completed: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    validation: str | None = None
    suggested_next_spec: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_tool_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "cancelled": self.cancelled,
            "summary": self.summary,
        }
        if self.needs_followup:
            payload["needs_followup"] = self.needs_followup
        if self.phase_boundary:
            payload["phase_boundary"] = self.phase_boundary
        if self.followup_reason is not None:
            payload["followup_reason"] = self.followup_reason
        if self.recoverable:
            payload["recoverable"] = self.recoverable
        if self.completed:
            payload["completed"] = list(self.completed)
        if self.remaining:
            payload["remaining"] = list(self.remaining)
        if self.modified_files:
            payload["modified_files"] = list(self.modified_files)
        if self.validation is not None:
            payload["validation"] = self.validation
        if self.suggested_next_spec is not None:
            payload["suggested_next_spec"] = self.suggested_next_spec
        if self.extras:
            payload["extras"] = self.extras
        return payload

    @classmethod
    def from_tool_payload(cls, data: dict[str, Any]) -> "WorkerDispatchResult":
        """Restore a dispatch result from a planner tool payload."""
        return cls(
            ok=bool(data.get("ok", False)),
            summary=str(data.get("summary", "")),
            cancelled=bool(data.get("cancelled", False)),
            needs_followup=bool(data.get("needs_followup", False)),
            phase_boundary=bool(data.get("phase_boundary", False)),
            followup_reason=(
                str(data["followup_reason"]) if data.get("followup_reason") is not None else None
            ),
            recoverable=bool(data.get("recoverable", False)),
            completed=_string_list(data.get("completed")),
            remaining=_string_list(data.get("remaining")),
            modified_files=_string_list(data.get("modified_files")),
            validation=str(data["validation"]) if data.get("validation") is not None else None,
            suggested_next_spec=(
                str(data["suggested_next_spec"]) if data.get("suggested_next_spec") is not None else None
            ),
            extras=data.get("extras") if isinstance(data.get("extras"), dict) else {},
        )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


DispatchCallback = Callable[[str, WorkerDispatchRequest], WorkerDispatchResult]
"""Called from the planner's worker thread.

Args: (tool_call_id, request). Blocks until the GUI/user has approved or
cancelled the dispatch and (if approved) the worker manager has finished.
"""


__all__ = [
    "WorkerDispatchRequest",
    "WorkerDispatchResult",
    "DispatchCallback",
]
