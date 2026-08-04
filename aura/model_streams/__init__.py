"""Model stream registry — backend stream registration for model generation.

The singleton ``model_streams`` is the central registry that maps named streams
to backend handlers.

``PRODUCTION_STREAM_HOOK`` is the canonical hook for normal Aura coding: one
continuous production model that owns the user's request from inspection through
validation.
"""

from __future__ import annotations

from aura.model_streams.registry import ModelStreamRegistry, model_streams

PRODUCTION_STREAM_HOOK = "generate_production_code"

__all__ = [
    "ModelStreamRegistry",
    "model_streams",
    "PRODUCTION_STREAM_HOOK",
]
