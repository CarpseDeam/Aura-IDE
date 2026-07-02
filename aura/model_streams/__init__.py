"""Model stream registry — backend stream registration for planner/worker generation.

The singleton ``model_streams`` is the central registry that maps named streams
(e.g. ``generate_planner_code``, ``generate_worker_code``) to backend handlers.
"""

from __future__ import annotations

from aura.model_streams.registry import ModelStreamRegistry, model_streams

__all__ = ["ModelStreamRegistry", "model_streams"]
