from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["LapResult"]


@dataclass(frozen=True)
class LapResult:
    """Result of one unattended planner→worker lap.

    Attributes:
        has_work: True if the git working tree changed during the pass.
        summary: Human-readable one-line description of what changed.
        changed_files: Tuple of workspace-relative paths that were modified.
    """
    has_work: bool
    summary: str
    changed_files: tuple[str, ...]
    worker_ok: bool = True
    worker_status: str = "completed"
    worker_errors: list[str] = field(default_factory=list)
    validation_results: list[dict] = field(default_factory=list)
