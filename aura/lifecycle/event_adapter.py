"""Event adapter — bridge EventBus facts into lifecycle notify hooks."""

from __future__ import annotations

import logging
from typing import Callable

from aura.events.bus import EventBus
from aura.events.event import AuraEvent
from aura.events.topics import ALL
from aura.lifecycle.context import HookContext
from aura.lifecycle.registry import LifecycleHooks

_log = logging.getLogger(__name__)


def attach_lifecycle_notify(
    bus: EventBus,
    lifecycle: LifecycleHooks,
) -> Callable[[], None]:
    """Subscribe to **all** EventBus facts and forward them as notify hooks.

    For every ``AuraEvent`` emitted on *bus*, this adapter builds a
    ``HookContext`` via :meth:`HookContext.from_event` and delivers it to
    ``lifecycle.notify()``.

    Returns:
        An unsubscribe callable.  Call it to detach the adapter and stop
        forwarding events.
    """

    def _on_event(event: AuraEvent) -> None:
        ctx = HookContext.from_event(event, category="notify")
        lifecycle.notify(ctx)

    unsub = bus.subscribe(ALL, _on_event)
    _log.debug("attached lifecycle notify adapter to EventBus")
    return unsub
