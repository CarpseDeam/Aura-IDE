"""GateDecision — the result contract for gate hook handlers."""

from __future__ import annotations

from typing import Any


class GateDecision:
    """Result returned by a gate hook handler.

    A gate decision can *allow* (default), *block*, *rewrite* the payload,
    or *inject* additional context.  The :class:`GateHookRegistry` composes
    multiple decisions according to the lifecycle gate contract.
    """

    def __init__(
        self,
        *,
        allowed: bool = True,
        blocked: bool = False,
        reason: str = "",
        severity: str = "info",
        updated_payload: dict[str, Any] | None = None,
        additional_context: str = "",
        force_continue: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.allowed = allowed
        self.blocked = blocked
        self.reason = reason
        self.severity = severity
        self.updated_payload = updated_payload
        self.additional_context = additional_context
        self.force_continue = force_continue
        self.metadata: dict[str, Any] = metadata if metadata is not None else {}

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
    def force_continue(cls, reason: str) -> "GateDecision":
        """Return a decision that forces continuation regardless of other
        handlers."""
        return cls(allowed=True, blocked=False, force_continue=True, reason=reason)
