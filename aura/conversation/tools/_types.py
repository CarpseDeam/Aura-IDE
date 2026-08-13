"""Data types shared across the tools subsystem."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

ApprovalAction = Literal["approve", "reject", "reject_all", "approve_all"]


@dataclass(frozen=True)
class ApprovalFileChange:
    """One file's before/after content within an approval request."""

    rel_path: str
    old_content: str
    new_content: str
    is_new_file: bool = False


@dataclass
class ApprovalRequest:
    """Passed to approval_cb when a write is proposed.

    ``rel_path``/``old_content``/``new_content``/``is_new_file`` describe the
    single file every existing caller (write_file, delete_file, MCP/dynamic
    consequential calls) proposes. A multi-file ``patch_file`` transaction
    additionally sets ``changes`` to the complete ordered set of files the one
    user decision covers; ``rel_path``/``old_content``/``new_content`` then
    mirror its first entry so single-file-shaped consumers keep working.
    """

    tool_name: str
    rel_path: str
    old_content: str
    new_content: str
    is_new_file: bool
    approval_id: str = ""
    approval_timeout_seconds: float = 0.0
    changes: tuple[ApprovalFileChange, ...] | None = None

    @property
    def file_changes(self) -> tuple[ApprovalFileChange, ...]:
        """The complete ordered set of files this one decision covers.

        Derives a single-entry tuple from the top-level fields when
        ``changes`` was not supplied, so every caller can iterate one
        truthful list regardless of how the request was built.
        """
        if self.changes:
            return self.changes
        return (
            ApprovalFileChange(
                self.rel_path, self.old_content, self.new_content, self.is_new_file
            ),
        )

    @property
    def is_multi_file(self) -> bool:
        return self.changes is not None and len(self.changes) > 1


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
