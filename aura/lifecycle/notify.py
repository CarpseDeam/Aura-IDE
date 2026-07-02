"""NotifyHookRegistry — observe lifecycle facts (fire-and-forget)."""

from __future__ import annotations

import logging
from typing import Callable

from aura.lifecycle.context import HookContext
from aura.lifecycle.handlers import HandlerRecord
from aura.lifecycle.matchers import HookMatcher

_log = logging.getLogger(__name__)


class NotifyHookRegistry:
    """Registry of notify (observe-only) lifecycle hooks.

    Notify handlers observe lifecycle facts.  They cannot alter control
    flow — their return values are ignored and exceptions are isolated.
    """

    def __init__(self) -> None:
        self._handlers: list[HandlerRecord] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        matcher: HookMatcher,
        handler: Callable[[HookContext], None],
        *,
        name: str = "",
        source: str = "internal",
    ) -> Callable[[], None]:
        """Register a notify handler.

        Args:
            matcher: Decides which contexts trigger this handler.
            handler: Called with the ``HookContext`` when matched.
            name: Optional human-readable label (defaults to repr of handler).
            source: Origin label — ``"internal"``, ``"user"``, etc.

        Returns:
            An unsubscribe callable.  Call it to remove the handler.
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
    # Notification
    # ------------------------------------------------------------------

    def notify(self, ctx: HookContext) -> None:
        """Deliver *ctx* to every matching handler in registration order.

        * Handler return values are ignored.
        * Handler exceptions are caught and logged — one failing handler
          never prevents subsequent handlers from running.
        """
        for record in self._handlers:
            if record.matcher.matches(ctx):
                try:
                    record.callback(ctx)
                except Exception:
                    _log.exception(
                        "notify_handler_error name=%s topic=%s",
                        record.name,
                        ctx.topic,
                    )
