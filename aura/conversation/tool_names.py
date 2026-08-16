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
    "edit_godot_scene",
    "edit_godot_editor",
    "edit_godot_asset_preview",
    "install_godot_editor_bridge",
}

__all__ = ["WRITE_TOOLS"]
