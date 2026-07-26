"""Model stream registry — backend stream registration for model generation.

The singleton ``model_streams`` is the central registry that maps named streams
to backend handlers.

``PRODUCTION_STREAM_HOOK`` is the canonical hook for normal Aura coding: one
continuous production model that owns the user's request from inspection through
validation.  ``PLANNER_STREAM_HOOK`` and ``WORKER_STREAM_HOOK`` remain
registered as compatibility scaffolding for historical dispatch paths; the
normal application path never triggers them.
"""

from __future__ import annotations

from aura.model_streams.registry import ModelStreamRegistry, model_streams

PRODUCTION_STREAM_HOOK = "generate_production_code"
PLANNER_STREAM_HOOK = "generate_planner_code"
WORKER_STREAM_HOOK = "generate_worker_code"

__all__ = [
    "ModelStreamRegistry",
    "model_streams",
    "PRODUCTION_STREAM_HOOK",
    "PLANNER_STREAM_HOOK",
    "WORKER_STREAM_HOOK",
]
