"""Dynamic-tool registry — scans .aura/tools/ for user-defined tool scripts.

A dynamic tool is a ``.py`` file dropped into the workspace, so its name is
whatever the file says it is.  That makes name collisions the whole security
question for this registry, and they are refused rather than resolved.

A script named after a built-in cannot shadow it: the executor dispatches
built-ins first, so the script would be unreachable anyway — but its *schema*
would still be published to the model under the built-in's name, and preflight
would then validate real ``write_file`` calls against the script's parameters
and reject every one of them.  A file in the workspace must not be able to
disable the write path.

Two scripts claiming one name are refused the same way, in sorted filename
order, so which tool answers a name does not depend on directory iteration
order or on which file was edited most recently.

Refusals are recorded in :attr:`collisions` rather than being silent, because
"my tool never shows up" is otherwise indistinguishable from a parse error.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.conversation.tools.dynamic import parse_tool_schema
from aura.conversation.tools.effects import (
    DEFAULT_EXTENSIBLE_TOOL_EFFECT,
    ToolEffect,
    parse_dynamic_effect_metadata,
)


def _builtin_tool_names() -> frozenset[str]:
    """Names the built-in catalog owns. Read at call time (import cycle)."""
    from aura.conversation.tools.registry import TOOL_HANDLERS

    return frozenset(TOOL_HANDLERS)


class DynamicToolRegistry:
    """Scans .aura/tools/, caches schemas by mtime, and provides tool lookup."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._cache: dict[str, Path] = {}       # tool_name -> file_path
        self._mtime_cache: dict[str, float] = {}  # str(file_path) -> mtime
        self._name_cache: dict[str, str] = {}     # str(file_path) -> declared name
        # file path -> why its declared name was refused.
        self._collisions: dict[str, str] = {}

    @property
    def collisions(self) -> dict[str, str]:
        """Scripts whose declared name was refused, and why."""
        return dict(self._collisions)

    def set_workspace_root(self, root: Path) -> None:
        """Update workspace root and clear all cached state."""
        self._workspace_root = root
        self._cache.clear()
        self._mtime_cache.clear()
        self._name_cache.clear()
        self._collisions.clear()

    def scan(self) -> dict[str, Path]:
        """Scan .aura/tools/ for .py files and map tool names to file paths.

        The name map is rebuilt from the full directory listing on every scan,
        in sorted filename order, so a claimed name is resolved against every
        other file present rather than against whichever files happen to have
        changed.  Per-file mtime caching still avoids re-parsing unchanged
        sources — it caches the parse, not the decision.

        Returns a dict of {tool_name: file_path}.
        """
        tools_dir = self._workspace_root / ".aura" / "tools"
        if not tools_dir.is_dir():
            self._cache.clear()
            self._mtime_cache.clear()
            self._collisions.clear()
            return {}

        builtins = _builtin_tool_names()
        resolved: dict[str, Path] = {}
        collisions: dict[str, str] = {}
        fresh_mtimes: dict[str, float] = {}

        for entry in sorted(tools_dir.iterdir()):
            if not entry.is_file() or entry.suffix != ".py":
                continue
            if entry.name.startswith("_"):
                continue

            key = str(entry)
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue

            cached_name = self._name_cache.get(key)
            if cached_name is not None and self._mtime_cache.get(key) == mtime:
                name = cached_name
            else:
                try:
                    name = parse_tool_schema(entry)["function"]["name"]
                except (ValueError, SyntaxError, OSError):
                    continue
            fresh_mtimes[key] = mtime
            self._name_cache[key] = name

            if name in builtins:
                collisions[key] = (
                    f"declares the built-in tool name '{name}'; a workspace "
                    "script cannot shadow or redefine a built-in tool"
                )
                continue
            if name in resolved:
                collisions[key] = (
                    f"declares tool name '{name}', already claimed by "
                    f"{resolved[name].name}"
                )
                continue
            resolved[name] = entry

        self._cache = resolved
        self._mtime_cache = fresh_mtimes
        self._name_cache = {
            key: name for key, name in self._name_cache.items() if key in fresh_mtimes
        }
        self._collisions = collisions
        return dict(self._cache)

    def schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI tool schemas for all currently cached dynamic tools."""
        schemas: list[dict[str, Any]] = []
        for file_path in self.scan().values():
            try:
                schema = parse_tool_schema(file_path)
                schemas.append(schema)
            except (ValueError, SyntaxError):
                pass
        return schemas

    def get(self, name: str) -> Path | None:
        """Return the file path for a dynamic tool by name, or None."""
        self.scan()  # ensure cache is fresh
        return self._cache.get(name)

    def effect(self, name: str) -> ToolEffect | None:
        """Effect a dynamic tool *declares* via ``AURA_TOOL_EFFECT``, or None.

        None means the script declared no valid metadata.  This is the raw
        declaration; :meth:`resolved_effect` is the authoritative answer.
        """
        path = self.get(name)
        if path is None:
            return None
        return parse_dynamic_effect_metadata(path)

    def resolved_effect(self, name: str) -> ToolEffect | None:
        """Authoritative effect for a registered dynamic tool, or None if unknown.

        A registered script always resolves to *some* effect: its declaration
        when it made one, otherwise the fail-safe consequential default — an
        undeclared script is arbitrary local code, and nothing about its
        presence establishes it as read-only.  ``None`` means only that this
        registry has no such tool.
        """
        path = self.get(name)
        if path is None:
            return None
        declared = parse_dynamic_effect_metadata(path)
        return declared if declared is not None else DEFAULT_EXTENSIBLE_TOOL_EFFECT
