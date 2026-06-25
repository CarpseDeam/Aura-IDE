from __future__ import annotations

import logging
from pathlib import Path

from aura.hazard.reader import GraduatedHazard, read_graduated

logger = logging.getLogger(__name__)


def format_guards(hazards: list[GraduatedHazard], limit: int = 5) -> str:
    if not hazards:
        return ""
    top = hazards[:limit]
    lines = ["### Learned Hazard Guards"]
    for h in top:
        error = h.representative_error or ""
        if len(error) > 200:
            error = error[:200] + "..."
        files = (
            ", ".join(h.sample_target_files[:5])
            if h.sample_target_files
            else "(various files)"
        )
        kind = h.task_kind if h.task_kind else "unknown"
        lines.append(
            f"{h.model} has burned {h.distinct_dispatch_count}\u00d7 on {kind} terrain "
            f"with: {error}. "
            f"This terrain is a known biter; verify the relevant behavior actually runs "
            f"before calling it done. Surfaces in: {files}."
        )
    return "\n".join(lines)


def _paths_related(a: str, b: str) -> bool:
    """Return True if two workspace paths share a common non-root directory
    prefix (>=1 component) or one is a parent directory of the other."""
    a_parts = Path(a).parent.parts
    b_parts = Path(b).parent.parts
    if not a_parts or not b_parts:
        return False
    common = 0
    for pa, pb in zip(a_parts, b_parts):
        if pa == pb:
            common += 1
        else:
            break
    return common >= 1 or Path(a).parent == Path(b) or Path(b).parent == Path(a)


def select_relevant_hazards(
    hazards: list[GraduatedHazard],
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    limit: int = 5,
) -> list[GraduatedHazard]:
    """Select hazards relevant to the given terrain context.

    When any terrain argument is provided, only hazards with at least one
    relevance signal are returned, scored and ranked.  When no terrain
    arguments are provided, falls back to top hazards by dispatch count.
    """
    has_terrain = model is not None or task_kind is not None or target_files

    if not has_terrain:
        return sorted(
            hazards,
            key=lambda h: (-h.distinct_dispatch_count, h.fingerprint),
        )[:limit]

    scored: list[tuple[int, int, str, GraduatedHazard]] = []
    for h in hazards:
        score = 0
        # Model match
        if model is not None and h.model == model:
            score += 2
        # Task kind match
        if task_kind is not None and h.task_kind == task_kind:
            score += 2
        # File overlap
        if target_files and h.sample_target_files:
            overlap = 0
            for tf in target_files:
                for sf in h.sample_target_files:
                    if _paths_related(tf, sf):
                        overlap += 1
                        if overlap >= 2:
                            break
                if overlap >= 2:
                    break
            score += min(overlap, 2)

        if score == 0:
            continue
        scored.append((-score, -h.distinct_dispatch_count, h.fingerprint, h))

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [h for _, _, _, h in scored[:limit]]


def build_hazard_guard_context(
    workspace_root: str | Path,
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    limit: int = 5,
) -> str:
    """Build optional hazard guard context block.

    When terrain arguments (model, task_kind, target_files) are provided,
    uses select_relevant_hazards to pick the most relevant hazards for the
    given terrain.  Otherwise falls back to top hazards by dispatch count.

    Returns empty string on any failure — this is optional context
    that must never propagate into the caller.
    """
    try:
        hazards = read_graduated(workspace_root)
        has_terrain = model is not None or task_kind is not None or target_files
        if has_terrain:
            selected = select_relevant_hazards(
                hazards,
                model=model,
                task_kind=task_kind,
                target_files=target_files,
                limit=limit,
            )
        else:
            selected = hazards
        return format_guards(selected, limit=limit)
    except Exception:
        logger.debug("Hazard guard context unavailable", exc_info=True)
        return ""

