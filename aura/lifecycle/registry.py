"""LifecycleHooks — top-level facade owning one notify + one gate registry."""

from __future__ import annotations

from typing import Callable

from aura.lifecycle.context import HookContext
from aura.lifecycle.decisions import GateDecision
from aura.lifecycle.gates import GateHookRegistry
from aura.lifecycle.matchers import HookMatcher
from aura.lifecycle.notify import NotifyHookRegistry


class LifecycleHooks:
    """Top-level facade for the lifecycle hooks system.

    Owns one :class:`NotifyHookRegistry` and one :class:`GateHookRegistry`.
    This is the single entry-point callers use to register handlers and
    drive lifecycle events.
    """

    def __init__(self) -> None:
        self._notify_registry = NotifyHookRegistry()
        self._gate_registry = GateHookRegistry()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_notify(
        self,
        matcher: HookMatcher,
        handler: Callable[[HookContext], None],
        *,
        name: str = "",
        source: str = "internal",
    ) -> Callable[[], None]:
        """Register a notify (observe) handler.  Returns an unsubscriber."""
        return self._notify_registry.register(
            matcher, handler, name=name, source=source
        )

    def register_gate(
        self,
        matcher: HookMatcher,
        handler: Callable[[HookContext], GateDecision],
        *,
        name: str = "",
        source: str = "internal",
    ) -> Callable[[], None]:
        """Register a gate (decide) handler.  Returns an unsubscriber."""
        return self._gate_registry.register(
            matcher, handler, name=name, source=source
        )

    def notify(self, ctx: HookContext) -> None:
        """Deliver *ctx* to matching notify handlers."""
        self._notify_registry.notify(ctx)

    async def ask(self, ctx: HookContext) -> GateDecision:
        """Evaluate matching gate handlers for *ctx*."""
        return await self._gate_registry.ask(ctx)
