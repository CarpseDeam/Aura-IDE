"""Lifecycle hooks — the deterministic attachment and control layer for Aura.

This package provides the foundation for harness-wide lifecycle extension:

* **HookContext** — immutable context snapshot passed to every handler.
* **HookMatcher** — deterministic topic/phase/role/tool matching.
* **Handler record** — registration metadata for future extension.
* **NotifyHookRegistry** — observe lifecycle facts (fire-and-forget).
* **GateHookRegistry** — decide at named checkpoints (allow / block / rewrite).
* **LifecycleHooks** — top-level facade owning one notify + one gate registry.
"""

from __future__ import annotations

from aura.lifecycle.context import HookContext
from aura.lifecycle.decisions import GateDecision
from aura.lifecycle.event_adapter import attach_lifecycle_notify
from aura.lifecycle.gates import GateHookRegistry
from aura.lifecycle.handlers import HandlerRecord
from aura.lifecycle.matchers import HookMatcher
from aura.lifecycle.notify import NotifyHookRegistry
from aura.lifecycle.registry import LifecycleHooks

__all__ = [
    "GateDecision",
    "GateHookRegistry",
    "HandlerRecord",
    "HookContext",
    "HookMatcher",
    "LifecycleHooks",
    "NotifyHookRegistry",
    "attach_lifecycle_notify",
]
