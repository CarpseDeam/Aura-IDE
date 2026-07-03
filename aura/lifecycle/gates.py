"""GateHookRegistry — decide at named checkpoints (allow / block / rewrite)."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from aura.lifecycle.context import HookContext
from aura.lifecycle.decisions import GateDecision
from aura.lifecycle.handlers import HandlerRecord
from aura.lifecycle.matchers import HookMatcher

_log = logging.getLogger(__name__)

# Sentinel for "no rewrite applied yet".
_NO_REWRITE = object()


class GateHookRegistry:
    """Registry of gate (decision-point) lifecycle hooks.

    Gate handlers answer the question: "should this operation proceed?"

    Contract:

    * No matching handlers → allow.
    * Matching handlers run in registration order.
    * Any ``blocked`` wins.
    * ``additional_context`` values are concatenated in registration order.
    * ``force_continue`` is preserved when any handler requests it.
    * Exactly one ``updated_payload`` rewrite applies per gate call.
    * Multiple rewrites produce a blocked decision with reason
      ``"lifecycle_gate_multiple_rewriters"``.
    * Handler exceptions produce a blocked decision with reason
      ``"lifecycle_gate_handler_error"`` and are logged.
    """

    def __init__(self) -> None:
        self._handlers: list[HandlerRecord] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        matcher: HookMatcher,
        handler: Callable[[HookContext], GateDecision],
        *,
        name: str = "",
        source: str = "internal",
    ) -> Callable[[], None]:
        """Register a gate handler.

        Args:
            matcher: Decides which contexts trigger this handler.
            handler: Called with the ``HookContext``; must return a
                :class:`GateDecision`.
            name: Optional human-readable label.
            source: Origin label — ``"internal"``, ``"user"``, etc.

        Returns:
            An unsubscribe callable.
        """
        record = HandlerRecord(
            name=name or repr(handler),
            matcher=matcher,
            callback=handler,
            handler_kind="python",
            source=source,
        )
        self._handlers.append(record)
        return lambda: self._remove(record)

    def _remove(self, record: HandlerRecord) -> None:
        """Remove a previously registered handler record."""
        try:
            self._handlers.remove(record)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    async def ask(self, ctx: HookContext) -> GateDecision:
        """Evaluate all matching gate handlers and return a composed decision.

        Returns:
            A :class:`GateDecision` reflecting the composed result of all
            matching handlers.
        """
        matching = [r for r in self._handlers if r.matcher.matches(ctx)]

        # Fast path: no handlers match → allow.
        if not matching:
            return GateDecision.allow()

        # Compose results.
        rewrite: object = _NO_REWRITE
        rewrite_reason = ""
        context_parts: list[str] = []
        blocked = False
        block_reason = ""
        block_severity = "error"
        block_metadata: dict[str, Any] = {}
        force_continue = False

        for record in matching:
            try:
                result: Any = record.callback(ctx)

                # Allow awaiting coroutines and other awaitable handler results.
                if inspect.isawaitable(result):
                    result = await result

            except Exception:
                _log.exception(
                    "gate_handler_error name=%s topic=%s",
                    record.name,
                    ctx.topic,
                )
                return GateDecision.block(
                    reason="lifecycle_gate_handler_error",
                    severity="error",
                )

            if not isinstance(result, GateDecision):
                _log.error(
                    "gate_handler_invalid_decision name=%s topic=%s result_type=%s",
                    record.name,
                    ctx.topic,
                    type(result).__name__,
                )
                return GateDecision.block(
                    reason="lifecycle_gate_invalid_decision",
                    severity="error",
                )

            decision = result

            # --- Compose ---

            if decision.blocked:
                blocked = True
                if block_reason:
                    block_reason += "; " + decision.reason
                else:
                    block_reason = decision.reason
                block_severity = decision.severity
                block_metadata.update(decision.metadata)

            if decision.additional_context:
                context_parts.append(decision.additional_context)

            if decision.force_continue:
                force_continue = True

            if decision.updated_payload is not None:
                if rewrite is not _NO_REWRITE:
                    # Multiple rewrites → blocked.
                    return GateDecision.block(
                        reason="lifecycle_gate_multiple_rewriters",
                        severity="error",
                    )
                rewrite = decision.updated_payload
                rewrite_reason = decision.reason

        # Build the composed result.
        if blocked:
            return GateDecision(
                allowed=False,
                blocked=True,
                reason=block_reason,
                severity=block_severity,
                additional_context="\n".join(context_parts) if context_parts else "",
                force_continue=force_continue,
                metadata=block_metadata,
            )

        additional_context = "\n".join(context_parts) if context_parts else ""

        if rewrite is not _NO_REWRITE:
            return GateDecision(
                allowed=True,
                blocked=False,
                updated_payload=rewrite,  # type: ignore[arg-type]
                reason=rewrite_reason,
                additional_context=additional_context,
                force_continue=force_continue,
            )

        return GateDecision(
            allowed=True,
            blocked=False,
            additional_context=additional_context,
            force_continue=force_continue,
        )
