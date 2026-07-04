"""Small data types for runtime context composition."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeRole(str, Enum):
    PLANNER = "planner"
    WORKER = "worker"
    SINGLE = "single"

    @classmethod
    def from_value(cls, value: "RuntimeRole | str") -> "RuntimeRole":
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"all", "default"}:
            normalized = cls.PLANNER.value
        return cls(normalized)


@dataclass(frozen=True)
class ContextSource:
    source_id: str
    kind: str
    roles: tuple[RuntimeRole, ...]
    reason: str
    origin_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextLedgerEntry:
    source_id: str
    kind: str
    role: RuntimeRole
    reason: str
    included: bool
    char_count: int
    error: str | None = None


@dataclass(frozen=True)
class ComposedContext:
    role: RuntimeRole
    system_prompt: str
    context_text: str
    ledger: tuple[ContextLedgerEntry, ...]
