"""Neutral tool-name sets used for execution and receipt bookkeeping.

These are descriptive name sets, not policy: nothing here counts calls, caps
them, or decides whether the model may act. ``WRITE_TOOLS`` names the built-in
tools whose successful result means the workspace changed, which is what the
approval "reject all writes" bookkeeping and the write-tracking event relay
need to know.
"""
from __future__ import annotations

WRITE_TOOLS = {
    "apply_patch",
    "apply_agent_change_set",
}

__all__ = ["WRITE_TOOLS"]
