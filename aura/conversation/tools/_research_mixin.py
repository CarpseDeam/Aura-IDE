"""Mixin for ToolRegistry implementing research tool handlers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from aura.conversation.tools._types import ToolExecResult
from aura.research import research_current_info


class ResearchHandlersMixin:
    """Mixin for ToolRegistry implementing research tool handlers."""

    def _handle_research_current_info(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "query is required"},
            )
        constraints = args.get("constraints")
        result = research_current_info(query, constraints)
        return ToolExecResult(
            ok=result.ok,
            payload={
                "ok": result.ok,
                "query": result.query,
                "sources": [asdict(s) for s in result.sources],
                "evidence": [asdict(e) for e in result.evidence],
                "notes": result.notes,
            },
        )
