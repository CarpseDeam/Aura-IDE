from __future__ import annotations

import logging
from pathlib import Path

from aura.skills.models import Skill, SkillProvenance
from aura.skills.reader import read_skills
from aura.skills.selection import select_relevant_skills

logger = logging.getLogger(__name__)


def format_skills(skills: list[Skill], limit: int = 5) -> str:
    """Format skills into a context text block.

    Graduated skills are emitted under a "### Learned Hazard Guards" header
    (matching aura.hazard.guard_text.format_guards output), followed by
    bundled skills under a "### Bundled Skills" header.
    Returns empty string when no skills are provided.
    """
    if not skills:
        return ""
    top = skills[:limit]

    bundled = [s for s in top if s.provenance == SkillProvenance.BUNDLED]
    graduated = [s for s in top if s.provenance == SkillProvenance.FAILURE_GRADUATED]

    parts: list[str] = []
    if graduated:
        parts.append("### Learned Hazard Guards")
        parts.extend(s.text for s in graduated)
    if bundled:
        parts.append("### Bundled Skills")
        parts.extend(s.text for s in bundled)
    return "\n".join(parts)


def build_skill_context(
    workspace_root: str | Path,
    *,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    limit: int = 5,
) -> str:
    """Read, select, and format skills for the given terrain.

    Always returns a string (possibly empty). Never propagates exceptions —
    returns "" on any failure.
    """
    try:
        skills = read_skills(workspace_root)
        selected = select_relevant_skills(
            skills,
            task_kind=task_kind,
            target_files=target_files,
            limit=limit,
        )
        return format_skills(selected, limit=limit)
    except Exception:
        logger.debug("build_skill_context failed", exc_info=True)
        return ""
