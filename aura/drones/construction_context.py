from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass
class DroneConstructionContext:
    mode: str  # "new" or "existing"
    drone_id: str = ""


_context: DroneConstructionContext | None = None


def enter_drone_construction(mode: str, drone_id: str = "") -> None:
    global _context
    _context = DroneConstructionContext(mode=mode, drone_id=drone_id)


def clear_drone_construction() -> None:
    global _context
    _context = None


def is_drone_construction_active() -> bool:
    return _context is not None


def get_drone_construction_context() -> DroneConstructionContext | None:
    return _context


def build_construction_guide() -> str:
    """Read drone_construction.md and return formatted guide text, or empty string if context inactive."""
    if _context is None:
        return ""
    guide_path = Path(__file__).resolve().parent / "drone_construction.md"
    if not guide_path.exists():
        return ""
    try:
        content = guide_path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.debug("Failed to read drone_construction.md", exc_info=True)
        return ""
    mode_label = "Creating new Drone" if _context.mode == "new" else "Editing existing Drone"
    return (
        f"\n## Drone Construction Context ({mode_label})\n"
        f"The following specification governs this Drone build/edit. Follow it exactly.\n\n"
        f"{content}"
    )
