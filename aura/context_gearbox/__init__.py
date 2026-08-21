"""Runtime context composition for Aura."""
from __future__ import annotations

from aura.context_gearbox.models import (
    ComposedContext,
    ContextLedgerEntry,
    ContextSource,
)
from aura.context_gearbox.runtime import (
    READ_ONLY_COLLABORATION_INSTRUCTION,
    build_context_text,
    compose_system_prompt,
    context_gearbox_metadata,
    format_context_gearbox_display,
    format_prompt_composition,
    serialize_context_ledger,
    summarize_context_ledger,
)

__all__ = [
    "READ_ONLY_COLLABORATION_INSTRUCTION",
    "ContextSource",
    "ContextLedgerEntry",
    "ComposedContext",
    "compose_system_prompt",
    "build_context_text",
    "serialize_context_ledger",
    "summarize_context_ledger",
    "context_gearbox_metadata",
    "format_context_gearbox_display",
    "format_prompt_composition",
]
