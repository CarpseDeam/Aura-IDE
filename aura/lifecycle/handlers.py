"""Handler record — registration metadata for lifecycle hook handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from aura.lifecycle.matchers import HookMatcher


@dataclass(frozen=True, slots=True)
class HandlerRecord:
    """Metadata record for a registered lifecycle hook handler.

    This gives Aura a future path for user hooks, command hooks, HTTP hooks,
    and agent hooks without adding those handler runtimes in this patch.
    """

    name: str
    matcher: HookMatcher
    callback: Callable[..., Any]
    handler_kind: str = "python"
    source: str = "internal"
    metadata: dict[str, Any] | None = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
