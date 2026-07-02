"""GateDecision — the result contract for gate hook handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True, kw_only=True)
class GateDecision:
    """Result returned by a gate hook handler.

    A gate decision can *allow* (default), *block*, *rewrite* the payload,
    or *inject* additional context.  The :class:`GateHookRegistry` composes
    multiple decisions according to the lifecycle gate contract.
    """

    allowed: bool = True
    blocked: bool = False
    reason: str = ""
    severity: str = "info"
    updated_payload: dict[str, Any] | None = None
    additional_context: str = ""
    force_continue: bool = False
    metadata: dict[str, Any] | None = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.updated_payload is not None:
            object.__setattr__(
                self, "updated_payload", dict(self.updated_payload)
            )
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    # ------------------------------------------------------------------
    # Factory constructors
    # ------------------------------------------------------------------

    @classmethod
    def allow(cls) -> "GateDecision":
        """Return a decision that allows the operation to proceed."""
        return cls(allowed=True, blocked=False)

    @classmethod
    def block(cls, reason: str, severity: str = "error") -> "GateDecision":
        """Return a decision that blocks the operation."""
        return cls(allowed=False, blocked=True, reason=reason, severity=severity)

    @classmethod
    def rewrite(
        cls, updated_payload: dict[str, Any], reason: str = ""
    ) -> "GateDecision":
        """Return a decision that rewrites the payload in-flight."""
        return cls(
            allowed=True,
            blocked=False,
            updated_payload=updated_payload,
            reason=reason,
        )

    @classmethod
    def inject_context(
        cls, additional_context: str, reason: str = ""
    ) -> "GateDecision":
        """Return a decision that appends additional context."""
        return cls(
            allowed=True,
            blocked=False,
            additional_context=additional_context,
            reason=reason,
        )

    @classmethod
    def force_continuation(cls, reason: str) -> "GateDecision":
        """Return a decision that forces continuation regardless of other
        handlers."""
        return cls(allowed=True, blocked=False, force_continue=True, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict for serialisation or logging."""
        return {
            "allowed": self.allowed,
            "blocked": self.blocked,
            "reason": self.reason,
            "severity": self.severity,
            "updated_payload": (
                dict(self.updated_payload)
                if self.updated_payload is not None
                else None
            ),
            "additional_context": self.additional_context,
            "force_continue": self.force_continue,
            "metadata": dict(self.metadata),
        }
