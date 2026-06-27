"""Runtime context composition for Aura."""
from __future__ import annotations

from aura.context_gearbox.models import (
    ComposedContext,
    ContextLedgerEntry,
    ContextSource,
    RuntimeRole,
)
from aura.context_gearbox.runtime import (
    CONTEXT_PLACEHOLDER,
    PLANNER_SYSTEM_PROMPT,
    SINGLE_SYSTEM_PROMPT,
    WORKER_SYSTEM_PROMPT,
    build_context_text,
    context_gearbox_metadata,
    compose_system_prompt,
    default_role_prompt,
    format_context_gearbox_display,
    serialize_context_ledger,
    summarize_context_ledger,
)

__all__ = [
    "CONTEXT_PLACEHOLDER",
    "PLANNER_SYSTEM_PROMPT",
    "WORKER_SYSTEM_PROMPT",
    "SINGLE_SYSTEM_PROMPT",
    "RuntimeRole",
    "ContextSource",
    "ContextLedgerEntry",
    "ComposedContext",
    "default_role_prompt",
    "compose_system_prompt",
    "build_context_text",
    "serialize_context_ledger",
    "summarize_context_ledger",
    "context_gearbox_metadata",
    "format_context_gearbox_display",
]
