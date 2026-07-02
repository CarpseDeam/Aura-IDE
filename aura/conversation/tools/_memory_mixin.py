"""Mixin providing project memory handler methods for ToolRegistry.

Expected on self:
    _root: Path  (workspace root)
"""

from __future__ import annotations

from aura.conversation.tools._types import ToolExecResult
from aura.memory_db import ProjectMemoryDB


class MemoryHandlersMixin:
    """Handlers for project memory tools."""


    def _handle_search_project_memory(self, args, approval_cb, reject_all) -> ToolExecResult:
        """Handle search_project_memory tool call."""
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "query is required."},
            )
        top_k = int(args.get("top_k", 5))
        try:
            db = ProjectMemoryDB(self._root / ".aura" / "memory.db")
            results = db.search(query, top_k)
            if not results:
                return ToolExecResult(
                    ok=True,
                    payload={"ok": True, "message": "No matching memories found.", "results": []},
                )
            # Format results clearly
            lines: list[str] = []
            lines.append(f"Found {len(results)} result(s):\n")
            for r in results:
                lines.append(f"--- Memory #{r['id']} [{r.get('created_at', '?')}] ---")
                if r.get("metadata"):
                    lines.append(f"Metadata: {r['metadata']}")
                lines.append(r["content"])
                lines.append("")
            return ToolExecResult(
                ok=True,
                payload={
                    "ok": True,
                    "message": "\n".join(lines),
                    "results": results,
                },
            )
        except Exception as exc:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": f"Memory search failed: {exc}"},
            )

    def _handle_save_to_project_memory(self, args, approval_cb, reject_all) -> ToolExecResult:
        """Handle save_to_project_memory tool call."""
        content = str(args.get("content", "")).strip()
        if not content:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "content is required and must be non-empty."},
            )
        metadata = args.get("metadata")
        try:
            db = ProjectMemoryDB(self._root / ".aura" / "memory.db")
            memory_id = db.insert(content, metadata)
            return ToolExecResult(
                ok=True,
                payload={
                    "ok": True,
                    "message": f"Memory saved with ID #{memory_id}.",
                    "memory_id": memory_id,
                },
            )
        except Exception as exc:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": f"Failed to save memory: {exc}"},
            )
