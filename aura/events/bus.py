"""Event bus — publish / subscribe with subscriber isolation."""

from __future__ import annotations

import logging
from typing import Any, Callable

from aura.events.event import AuraEvent
from aura.events.topics import ALL

logger = logging.getLogger(__name__)

# Type alias for event handlers.
EventHandler = Callable[[AuraEvent], Any]


class EventBus:
    """Lightweight in-process event bus.

    Supports multiple subscribers per topic, wildcard (``*``) subscribers
    that receive every event, and safe subscriber isolation — a single
    failing subscriber never takes down the bus.

    Usage::

        bus = EventBus()

        def on_artifact(event: AuraEvent) -> None:
            print(f"  artifact {event.artifact_id} item {event.artifact_item_id}")

        unsub = bus.subscribe("work_artifact.item_ready", on_artifact)
        bus.emit(AuraEvent(topic="work_artifact.item_ready", artifact_id="art-1", artifact_item_id="item-1"))
        unsub()
    """

    def __init__(self) -> None:
        # Internal: topic → list of handlers.
        self._subscribers: dict[str, list[EventHandler]] = {}

    # ── public API ──────────────────────────────────────────────────────────

    def subscribe(self, topic: str, handler: EventHandler) -> Callable[[], None]:
        """Register *handler* for the given *topic*.

        Returns a zero-arg callable that unsubscribes this specific handler.
        """
        self._subscribers.setdefault(topic, []).append(handler)
        # Capture identity so we can remove the right handler later.
        _handlers = self._subscribers[topic]

        def _unsubscribe() -> None:
            if handler in _handlers:
                _handlers.remove(handler)

        return _unsubscribe

    def unsubscribe(self, topic: str, handler: EventHandler) -> None:
        """Remove a specific handler from a topic (no-op if not found)."""
        handlers = self._subscribers.get(topic)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def emit(self, event: AuraEvent) -> None:
        """Deliver *event* to every matching subscriber.

        Subscriber errors are caught and logged so one bad handler does not
        crash the bus.  *Explicit* (topic-specific) subscribers fire *before*
        wildcard subscribers so projector ordering is predictable.
        """
        seen: set[int] = set()

        # 1. Topic-specific subscribers.
        for handler in self._subscribers.get(event.topic, ()):
            _safe_invoke(handler, event)
            seen.add(id(handler))

        # 2. Wildcard subscribers (skip if already fired for this topic).
        if ALL in self._subscribers:
            for handler in self._subscribers[ALL]:
                if id(handler) not in seen:
                    _safe_invoke(handler, event)

    def subscriber_count(self, topic: str | None = None) -> int:
        """Number of handlers registered for *topic*, or total if *None*."""
        if topic is not None:
            return len(self._subscribers.get(topic, ()))
        return sum(len(hh) for hh in self._subscribers.values())

    def clear(self) -> None:
        """Remove all subscribers (useful in test teardown)."""
        self._subscribers.clear()


# ── internal helpers ────────────────────────────────────────────────────────

def _safe_invoke(handler: EventHandler, event: AuraEvent) -> None:
    """Call *handler(event)*, logging but swallowing any exception."""
    try:
        handler(event)
    except Exception:
        logger.exception(
            "EventBus: handler %r raised on topic=%r",
            getattr(handler, "__name__", handler),
            event.topic,
        )
