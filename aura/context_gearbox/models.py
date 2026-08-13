"""Small data types for runtime context composition."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextSource:
    source_id: str
    kind: str
    reason: str
    origin_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextLedgerEntry:
    source_id: str
    kind: str
    reason: str
    included: bool
    char_count: int
    error: str | None = None
    #: Optional per-entry classification detail, e.g. a skill's lifecycle state
    #: (``candidate_indexed`` / ``eager_guard`` / ``skipped``) or the
    #: aggregate skill-pack split (``index_chars=…; guard_chars=…``).
    detail: str | None = None


@dataclass(frozen=True)
class CustomPromptDiagnostics:
    """What a user's custom prompt adds to the canonical one, made visible.

    Nothing here rewrites the custom prompt. Verbatim canonical blocks are
    already dropped during composition; these fields describe what survives
    that, so a paraphrase of a rule that already has an owner is legible
    rather than silent.
    """

    char_count: int
    appended_char_count: int
    full_replacement: bool
    legacy_terms: tuple[str, ...]
    repeated_concepts: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return self.char_count == 0


@dataclass(frozen=True)
class ComposedContext:
    system_prompt: str
    context_text: str
    ledger: tuple[ContextLedgerEntry, ...]
