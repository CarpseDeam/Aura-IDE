"""The explicit Windows-server tool allowlist.

The server offers eighteen tools.  Aura exposes thirteen of them, named here
one at a time, because "whatever the server happens to ship" is not a surface
anyone reviewed — and a server upgrade must not silently hand the model a
screenshot pipe.  A name that is not in :data:`WINDOWS_MCP_ALLOWLIST` is not
registered, so it cannot be called, cannot be approved, and never reaches the
model's tool list.

**Structured semantics only.**  Everything exposed here drives the Windows UI
Automation tree: elements are addressed by name, ``automationId``, control
type, or a previously returned element id.  Nothing exposed here addresses the
screen by pixel.  That is the whole selection rule, and
:data:`WINDOWS_MCP_DENYLIST_REASONS` records why each rejected tool fails it.

**Effects are declared here, not inferred.**  The server annotates nothing
today — every tool arrives with only ``name``, ``description``, and
``inputSchema`` — so leaving effect resolution to the registry would fail every
one of them safe to ``COMMAND``, and the five genuine observations would be
withheld from read-only turns and would drag an approval prompt in front of a
window inspection.  The allowlist states each effect explicitly instead.

**A declaration can only make a tool safer, never freer.**  If a future server
release annotates ``ui_click`` as ``readOnlyHint``, that claim loses:
:func:`filter_windows_tool_defs` keeps whichever of the two effects is
consequential.  Aura's read of what a tool does is a floor, not a default.
"""
from __future__ import annotations

from typing import Any

from aura.conversation.tools.effects import (
    SCHEMA_EFFECT_KEY,
    ToolEffect,
    effect_from_metadata,
)

#: Capability id this server contributes while it is connected.  The request-
#: time context block keys off it, so the block exists exactly as long as the
#: tools do.
WINDOWS_MCP_CAPABILITY = "windows_computer_use"

#: Every tool Aura exposes from the Windows server, with the effect Aura holds
#: it to.  Observations are read-only inspections of the UIA tree; everything
#: else acts on another application and is consequential, so it goes through
#: the existing approval path.
#:
#: ``COMMAND`` rather than ``MUTATION`` for the consequential ones: these act
#: through a boundary the runtime cannot see, on an application Aura does not
#: own, and none of them is a workspace edit.  That is the same reasoning
#: behind :data:`~aura.conversation.tools.effects.DEFAULT_EXTENSIBLE_TOOL_EFFECT`.
WINDOWS_MCP_ALLOWLIST: dict[str, ToolEffect] = {
    # --- observation: inspects the UIA tree, changes nothing ---
    "ui_snapshot": ToolEffect.OBSERVATION,
    "ui_find": ToolEffect.OBSERVATION,
    "ui_read": ToolEffect.OBSERVATION,
    "ui_read_table": ToolEffect.OBSERVATION,
    "ui_wait": ToolEffect.OBSERVATION,
    # --- consequential: acts on another application ---
    "ui_click": ToolEffect.COMMAND,
    "ui_type": ToolEffect.COMMAND,
    "ui_select": ToolEffect.COMMAND,
    "ui_batch": ToolEffect.COMMAND,
    "window_management": ToolEffect.COMMAND,
    "app": ToolEffect.COMMAND,
    "file_open": ToolEffect.COMMAND,
    "file_save": ToolEffect.COMMAND,
}

#: Why each withheld tool is withheld.  Recorded rather than implied: "the
#: model cannot take a screenshot" is a property of this product, and the next
#: person to widen the allowlist should have to argue with a stated reason
#: instead of an absence.
WINDOWS_MCP_DENYLIST_REASONS: dict[str, str] = {
    "screenshot_control": (
        "captures pixels — screenshots and annotated screen images are exactly "
        "the vision surface this integration is defined to exclude"
    ),
    "mouse_control": (
        "drives the pointer by screen coordinate; acting on a position rather "
        "than on an element is the pixel-driven control path"
    ),
    "keyboard_control": (
        "raw synthetic keystrokes to whatever holds focus — the untargeted "
        "keyboard fallback, with no element and no structural result"
    ),
    "clipboard": (
        "not UI Automation at all, and reading a shared OS buffer silently "
        "exfiltrates whatever the user last copied"
    ),
    "ui_macro": (
        "replays steps stored under a name, so the approval prompt for a run "
        "shows the name and not the actions — the operator could not see what "
        "they were approving"
    ),
}

#: ``ui_read`` is a selector-addressed UIA text read that falls back to OCR
#: internally when a control refuses to yield text.  It is exposed because it
#: is the only way to read an element's value, and because the agent still
#: addresses an element rather than a region — but the fallback means a
#: successful result is not proof that the text came from the UIA tree.
UI_READ_OCR_FALLBACK_NOTE = (
    "ui_read may fall back to OCR when UIA text extraction fails; its result "
    "is element-addressed but not guaranteed to be structural."
)


class WindowsAllowlistError(ValueError):
    """The connected server does not offer the surface Aura expects."""


def _declared_effect(tool_def: dict[str, Any]) -> ToolEffect | None:
    """Effect the server itself claims, via ``x-aura-effect`` or annotations."""
    effect = effect_from_metadata(tool_def.get(SCHEMA_EFFECT_KEY))
    if effect is None:
        annotations = tool_def.get("annotations") or {}
        if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
            effect = ToolEffect.OBSERVATION
    return effect


def _effective_effect(name: str, tool_def: dict[str, Any]) -> ToolEffect:
    """Reconcile Aura's declared effect with the server's, safety-first.

    Aura's allowlist entry is the answer unless the server declares something
    *more* restrictive, in which case the server wins.  A server claiming a
    consequential tool is read-only never wins.
    """
    ours = WINDOWS_MCP_ALLOWLIST[name]
    theirs = _declared_effect(tool_def)
    if theirs is None:
        return ours
    if ours is ToolEffect.OBSERVATION and theirs is not ToolEffect.OBSERVATION:
        return theirs
    return ours


def filter_windows_tool_defs(
    tool_defs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only the allowlisted tools, each carrying its resolved effect.

    Selection, not rewriting.  Each surviving definition is a shallow copy of
    what the server sent, so ``description``, ``inputSchema``, ``annotations``,
    and any ``x-aura-effect`` the server supplied all reach registration
    unchanged; the only key this adds is ``x-aura-effect``, and only to say
    out loud what :func:`_effective_effect` resolved.

    Order follows :data:`WINDOWS_MCP_ALLOWLIST`, not the server's listing, so
    the exposed surface is stable across server releases.

    Raises :class:`WindowsAllowlistError` when the server offers none of the
    allowlisted tools.  That is not an empty surface to register quietly — it
    means this is not the server Aura thinks it is connected to.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for tool_def in tool_defs:
        if not isinstance(tool_def, dict):
            continue
        name = tool_def.get("name")
        if isinstance(name, str) and name in WINDOWS_MCP_ALLOWLIST:
            by_name[name] = tool_def

    kept: list[dict[str, Any]] = []
    for name in WINDOWS_MCP_ALLOWLIST:
        tool_def = by_name.get(name)
        if tool_def is None:
            continue
        allowed = dict(tool_def)
        allowed[SCHEMA_EFFECT_KEY] = _effective_effect(name, tool_def).value
        kept.append(allowed)

    if not kept:
        offered = sorted(
            str(d.get("name")) for d in tool_defs if isinstance(d, dict)
        )
        raise WindowsAllowlistError(
            "The connected server offers none of the allowlisted Windows UI "
            f"Automation tools. It offered: {offered or '<nothing>'}"
        )
    return kept


__all__ = [
    "UI_READ_OCR_FALLBACK_NOTE",
    "WINDOWS_MCP_ALLOWLIST",
    "WINDOWS_MCP_CAPABILITY",
    "WINDOWS_MCP_DENYLIST_REASONS",
    "WindowsAllowlistError",
    "filter_windows_tool_defs",
]
