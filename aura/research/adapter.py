"""Adapter from ResearchRequest to native web search execution.

The legacy browser/Drone web-research seam is retired.  Research requests
resolve through Aura's own search backend (DeepSeek's native Responses API
web search), independently of the selected chat model provider — the old
browser system is never launched and there is no fallback.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from aura.research.native import execute_native_web_search
from aura.research.ui_contract import (
    RESEARCH_UI_MODE_SILENT,
    with_research_ui_contract,
)

WEB_RESEARCH_DRONE_ID = "web-research"
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchAdapterCall:
    drone_id: str
    goal: str
    upstream: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_adapter_call(request: Any) -> ResearchAdapterCall:
    """Return the sync-runner call shape for a ResearchRequest-like object."""
    question = str(getattr(request, "question", "") or "").strip()
    route = str(getattr(request, "route", "answer_only") or "answer_only")
    ui_mode = str(getattr(request, "ui_mode", RESEARCH_UI_MODE_SILENT) or RESEARCH_UI_MODE_SILENT)
    request_dict = request.to_dict() if hasattr(request, "to_dict") else {}
    return ResearchAdapterCall(
        drone_id=WEB_RESEARCH_DRONE_ID,
        goal=question,
        upstream=with_research_ui_contract(
            {"research_request": request_dict},
            route=route,
            ui_mode=ui_mode,
        ),
    )


def execute_web_research_request(
    workspace_root: Path,
    request: Any,
    *,
    chat_provider: str | None = None,
    model: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Execute a research request through Aura's native web search backend.

    The legacy browser/Drone web-research path is retired.  Execution always
    resolves through the search backend's own credential; ``chat_provider``
    is recorded for tracing only and never gates the search.
    """
    question = str(getattr(request, "question", "") or "").strip()
    original = str(getattr(request, "original_text", "") or "").strip()
    if not question:
        return {
            "ok": False,
            "status": "invalid_request",
            "error": "research question is required",
        }

    result = execute_native_web_search(
        question=question,
        context=original or None,
        model=model,
        cancel_event=cancel_event,
        chat_provider=chat_provider,
    )
    return result.to_dict()
