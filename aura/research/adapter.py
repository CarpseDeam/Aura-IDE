"""Adapter from ResearchRequest to the existing web-research Drone seam."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from aura.drones.definition import DroneDefinition
from aura.drones.store import DroneStore
from aura.drones.sync_runner import run_read_only_drone_sync
from aura.research.ui_contract import (
    RESEARCH_UI_MODE_SILENT,
    with_research_ui_contract,
)

WEB_RESEARCH_DRONE_ID = "web-research"
_log = logging.getLogger(__name__)

Runner = Callable[..., dict[str, Any]]
DroneLoader = Callable[[Path, str], DroneDefinition | None]


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
    runner: Runner = run_read_only_drone_sync,
    drone_loader: DroneLoader = DroneStore.load_drone,
) -> dict[str, Any]:
    """Run the existing read-only web-research Drone for a request."""
    call = build_adapter_call(request)
    if not call.goal:
        return {
            "ok": False,
            "drone_id": call.drone_id,
            "error": "research question is required",
        }

    drone = drone_loader(Path(workspace_root), call.drone_id)
    if drone is None:
        folder = DroneStore.drone_folder(Path(workspace_root), call.drone_id)
        _log.warning(
            "answer_only_research_unregistered drone_id=%s folder=%s silent_requested=%s",
            call.drone_id,
            folder,
            bool(call.upstream.get("headless")),
        )
        return {
            "ok": False,
            "drone_id": call.drone_id,
            "drone_folder": str(folder),
            "error": "web-research Drone is not registered",
        }

    folder = DroneStore.drone_folder(Path(workspace_root), call.drone_id)
    _log.info(
        "answer_only_research_start drone_id=%s folder=%s silent_requested=%s",
        call.drone_id,
        folder,
        bool(call.upstream.get("headless")),
    )
    return runner(
        workspace_root=Path(workspace_root),
        drone_id=call.drone_id,
        drone=drone,
        goal=call.goal,
        upstream=call.upstream,
    )
