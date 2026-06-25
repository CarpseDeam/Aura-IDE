"""Data types shared across the tools subsystem."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

ApprovalAction = Literal["approve", "reject", "reject_all", "approve_all"]
RegistryMode = Literal["single", "planner", "worker"]


@dataclass
class ApprovalRequest:
    """Passed to approval_cb when a write is proposed."""

    tool_name: str
    rel_path: str
    old_content: str
    new_content: str
    is_new_file: bool
    approval_id: str = ""
    approval_timeout_seconds: float = 0.0


@dataclass
class ApprovalDecision:
    action: ApprovalAction
    note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


ApprovalCallback = Callable[[ApprovalRequest], ApprovalDecision]


@dataclass
class ToolExecResult:
    ok: bool
    payload: dict[str, Any]
    extras: dict[str, Any] = field(default_factory=dict)

    def to_tool_message_content(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False)
