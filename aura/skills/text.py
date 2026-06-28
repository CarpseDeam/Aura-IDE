from __future__ import annotations

import logging
from pathlib import Path

from aura.skills.eviction import (
    EvictionMode,
    apply_eviction_mode,
    compute_eviction_verdicts,
)
from aura.skills.models import Skill, SkillProvenance, compute_skill_id
from aura.skills.reader import read_skills
from aura.skills.selection import select_relevant_skills

logger = logging.getLogger(__name__)


def format_skills(skills: list[Skill], limit: int = 5) -> str:
    """Format skills into a context text block.

    Graduated skills are emitted under a "### Learned Hazard Guards" header
    (matching aura.hazard.guard_text.format_guards output), followed by
    reflection-refined skills and bundled skills.
    Returns empty string when no skills are provided.
    """
    if not skills:
        return ""
    top = skills[:limit]

    bundled = [s for s in top if s.provenance == SkillProvenance.BUNDLED]
    graduated = [s for s in top if s.provenance == SkillProvenance.FAILURE_GRADUATED]
    refined = [s for s in top if s.provenance == SkillProvenance.REFLECTION_REFINED]

    parts: list[str] = []
    if graduated:
        parts.append("### Learned Hazard Guards")
        parts.extend(s.text for s in graduated)
    if refined:
        parts.append("### Refined Skill Guards")
        parts.extend(s.text for s in refined)
    if bundled:
        parts.append("### Bundled Skills")
        parts.extend(s.text for s in bundled)
    return "\n".join(parts)


def build_skill_context_with_ids(
    workspace_root: str | Path,
    *,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    limit: int = 5,
    eviction_mode: EvictionMode | str = EvictionMode.OFF,
) -> tuple[str, list[str]]:
    """Like build_skill_context but also returns per-skill stable IDs.

    Returns a (text, [skill_id_1, skill_id_2, ...]) tuple.
    Always returns a tuple (possibly empty). Never propagates exceptions -
    returns ("", []) on any failure.
    """
    try:
        skills = read_skills(workspace_root)
        selected = select_relevant_skills(
            skills,
            task_kind=task_kind,
            target_files=target_files,
            limit=limit,
        )
        mode = EvictionMode.from_value(eviction_mode)
        if mode != EvictionMode.OFF:
            verdicts = compute_eviction_verdicts(
                Path(workspace_root),
                task_kind=task_kind,
            )
            selected = apply_eviction_mode(selected, verdicts, mode=mode)
        text = format_skills(selected, limit=limit)
        skill_ids = [compute_skill_id(s) for s in selected]
        return text, skill_ids
    except Exception:
        logger.debug("build_skill_context_with_ids failed", exc_info=True)
        return "", []


def build_skill_context(
    workspace_root: str | Path,
    *,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    limit: int = 5,
    eviction_mode: EvictionMode | str = EvictionMode.OFF,
) -> str:
    """Read, select, and format skills for the given terrain.

    Always returns a string (possibly empty). Never propagates exceptions —
    returns "" on any failure.
    """
    text, _ = build_skill_context_with_ids(
        workspace_root,
        task_kind=task_kind,
        target_files=target_files,
        limit=limit,
        eviction_mode=eviction_mode,
    )
    return text
