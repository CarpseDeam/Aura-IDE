"""Live research tool handlers."""
from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ToolExecResult
from aura.research.native import execute_native_web_search
from aura.research.result import format_research_answer


class ResearchHandlersMixin:
    """Handlers for configured external research capabilities."""

    def _handle_web_search(
        self,
        args: dict[str, Any],
        approval_cb: Any,
        reject_all: bool,
    ) -> ToolExecResult:
        question = str(args.get("question") or "").strip()
        context = str(args.get("context") or "").strip()
        if not question:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "question is required"},
            )

        result = execute_native_web_search(
            question=question,
            context=context or None,
            cancel_event=self.active_cancel_event,
        )
        payload = result.to_dict()
        payload["answer_for_chat"] = format_research_answer(result)
        return ToolExecResult(ok=bool(result.ok), payload=payload)


__all__ = ["ResearchHandlersMixin"]
