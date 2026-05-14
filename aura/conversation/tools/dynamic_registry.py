"""Dynamic-tool registry — scans .aura/tools/ for user-defined tool scripts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.conversation.tools.dynamic import parse_tool_schema


class DynamicToolRegistry:
    """Scans .aura/tools/, caches schemas by mtime, and provides tool lookup."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._cache: dict[str, Path] = {}       # tool_name -> file_path
        self._mtime_cache: dict[str, float] = {}  # str(file_path) -> mtime

    def set_workspace_root(self, root: Path) -> None:
        """Update workspace root and clear all cached state."""
        self._workspace_root = root
        self._cache.clear()
        self._mtime_cache.clear()

    def scan(self) -> dict[str, Path]:
        """Scan .aura/tools/ for .py files and map tool names to file paths.

        Uses per-file mtime caching to avoid re-parsing unchanged files.
        Returns a dict of {tool_name: file_path}.
        """
        tools_dir = self._workspace_root / ".aura" / "tools"
        if not tools_dir.is_dir():
            self._cache.clear()
            self._mtime_cache.clear()
            return {}

        current_files: set[str] = set()
        for entry in sorted(tools_dir.iterdir()):
            if not entry.is_file() or entry.suffix != ".py":
                continue
            if entry.name.startswith("_"):
                continue

            key = str(entry)
            current_files.add(key)
            mtime = entry.stat().st_mtime

            # Skip if unchanged since last parse
            if key in self._mtime_cache and self._mtime_cache[key] == mtime:
                continue

            try:
                schema = parse_tool_schema(entry)
                name = schema["function"]["name"]
                # Remove any old mapping for this file path (name may have changed)
                for old_name, old_path in list(self._cache.items()):
                    if str(old_path) == key:
                        del self._cache[old_name]
                        break
                self._cache[name] = entry
                self._mtime_cache[key] = mtime
            except (ValueError, SyntaxError):
                pass

        # Remove entries for files that no longer exist
        stale_keys = set(self._mtime_cache.keys()) - current_files
        for key in stale_keys:
            for name, path in list(self._cache.items()):
                if str(path) == key:
                    del self._cache[name]
                    break
            del self._mtime_cache[key]

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
