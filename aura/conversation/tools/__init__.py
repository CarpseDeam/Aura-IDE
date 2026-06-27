"""File-system tools (read-only and write) gated by approval callbacks."""
from aura.conversation.tools._types import (
    ApprovalDecision,
    ApprovalRequest,
    RegistryMode,
    ToolExecResult,
)
from aura.conversation.tools._schemas import (
    DISPATCH_TOOL_DEF,
)


def __getattr__(name: str):
    if name == "ToolRegistry":
        from aura.conversation.tools.registry import ToolRegistry

        return ToolRegistry
    raise AttributeError(name)

__all__ = [
    "ToolRegistry",
    "ApprovalDecision",
    "ApprovalRequest",
    "RegistryMode",
    "ToolExecResult",
    "DISPATCH_TOOL_DEF",
]
