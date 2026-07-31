from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from aura.skills.eviction import (
    EvictionMode,
    apply_eviction_mode,
    compute_eviction_verdicts,
)
from aura.skills.models import Skill, SkillProvenance, compute_skill_id, skill_label
from aura.skills.reader import read_skills
from aura.skills.selection import (
    DEFAULT_SKILL_LIMIT,
    has_terrain_signal,
    score_skills,
)

logger = logging.getLogger(__name__)

_MAX_REPORTED_SKIPPED = 20


@dataclass(frozen=True)
class SkillRecord:
    """One inspectable decision about a single skill for this turn."""

    skill_id: str
    label: str
    provenance: str
    reason: str
    char_count: int


@dataclass(frozen=True)
class SkillPack:
    """The skills selected for one turn plus why each was loaded or skipped."""

    text: str = ""
    selected: tuple[SkillRecord, ...] = field(default_factory=tuple)
    skipped: tuple[SkillRecord, ...] = field(default_factory=tuple)

    @property
    def skill_ids(self) -> list[str]:
        return [record.skill_id for record in self.selected]


def format_skills(skills: list[Skill], limit: int = DEFAULT_SKILL_LIMIT) -> str:
    """Format skills into a context text block.

    Sections follow scoping priority: workspace-authored standards, packaged
    skills, then learned hazard guards and refined guards.  Identical skill
    text is emitted at most once.
    Returns empty string when no skills are provided.
    """
    if not skills:
        return ""
    top = _dedupe_skills(skills)[:limit]

    authored = [s for s in top if s.provenance == SkillProvenance.USER_AUTHORED]
    bundled = [s for s in top if s.provenance == SkillProvenance.BUNDLED]
    graduated = [s for s in top if s.provenance == SkillProvenance.FAILURE_GRADUATED]
    refined = [s for s in top if s.provenance == SkillProvenance.REFLECTION_REFINED]

    parts: list[str] = []
    if authored:
        parts.append("### Project Engineering Standards")
        parts.extend(s.text for s in authored)
    if bundled:
        parts.append("### Bundled Skills")
        parts.extend(s.text for s in bundled)
    if graduated:
        parts.append("### Learned Hazard Guards")
        parts.extend(s.text for s in graduated)
    if refined:
        parts.append("### Refined Skill Guards")
        parts.extend(s.text for s in refined)
    return "\n".join(parts)


def _dedupe_skills(skills: list[Skill]) -> list[Skill]:
    """Drop repeats of identical skill text so nothing is injected twice."""
    seen: set[str] = set()
    unique: list[Skill] = []
    for skill in skills:
        key = compute_skill_id(skill)
        if key in seen:
            continue
        seen.add(key)
        unique.append(skill)
    return unique


def build_skill_pack(
    workspace_root: str | Path,
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    content: str | None = None,
    limit: int = DEFAULT_SKILL_LIMIT,
    eviction_mode: EvictionMode | str = EvictionMode.OFF,
) -> SkillPack:
    """Select this turn's skills and report why each was loaded or skipped.

    Selection is terrain-driven: with no terrain signal at all nothing is
    loaded, so a workspace-startup composition never dumps an unrelated pack
    into the prompt.  Never propagates exceptions — returns an empty pack.
    """
    try:
        skills = read_skills(workspace_root)
        if not skills:
            return SkillPack()
        if not has_terrain_signal(
            model=model,
            task_kind=task_kind,
            target_files=target_files,
            content=content,
        ):
            return SkillPack(
                skipped=tuple(
                    _record(skill, "no turn terrain to select against", 0)
                    for skill in skills[:_MAX_REPORTED_SKIPPED]
                )
            )

        ranked = score_skills(
            skills,
            model=model,
            task_kind=task_kind,
            target_files=target_files,
            content=content,
        )
        relevant = [item for item in ranked if item.is_relevant]
        chosen = _dedupe_skills([item.skill for item in relevant])[:limit]

        mode = EvictionMode.from_value(eviction_mode)
        if mode != EvictionMode.OFF:
            verdicts = compute_eviction_verdicts(
                Path(workspace_root),
                task_kind=task_kind,
            )
            chosen = apply_eviction_mode(chosen, verdicts, mode=mode)

        text = format_skills(chosen, limit=limit)
        chosen_ids = {compute_skill_id(skill) for skill in chosen}

        selected: list[SkillRecord] = []
        skipped: list[SkillRecord] = []
        for item in ranked:
            skill_id = compute_skill_id(item.skill)
            if skill_id in chosen_ids and not any(
                record.skill_id == skill_id for record in selected
            ):
                selected.append(
                    _record(
                        item.skill,
                        "selected: " + (", ".join(item.reasons) or "terrain match"),
                        len(item.skill.text),
                    )
                )
                continue
            if not item.is_relevant:
                reason = item.rejection_reason
            elif skill_id in chosen_ids:
                reason = "duplicate skill text already loaded"
            else:
                reason = (
                    f"ranked below the {limit}-skill limit "
                    f"(score {item.score})"
                )
            skipped.append(_record(item.skill, reason, 0))

        return SkillPack(
            text=text,
            selected=tuple(selected),
            skipped=tuple(skipped[:_MAX_REPORTED_SKIPPED]),
        )
    except Exception:
        logger.debug("build_skill_pack failed", exc_info=True)
        return SkillPack()


def _record(skill: Skill, reason: str, char_count: int) -> SkillRecord:
    return SkillRecord(
        skill_id=compute_skill_id(skill),
        label=skill_label(skill),
        provenance=skill.provenance.value,
        reason=reason,
        char_count=char_count,
    )


def build_skill_context_with_ids(
    workspace_root: str | Path,
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    content: str | None = None,
    limit: int = DEFAULT_SKILL_LIMIT,
    eviction_mode: EvictionMode | str = EvictionMode.OFF,
) -> tuple[str, list[str]]:
    """Like build_skill_context but also returns per-skill stable IDs.

    Returns a (text, [skill_id_1, skill_id_2, ...]) tuple.
    Always returns a tuple (possibly empty). Never propagates exceptions -
    returns ("", []) on any failure.
    """
    pack = build_skill_pack(
        workspace_root,
        model=model,
        task_kind=task_kind,
        target_files=target_files,
        content=content,
        limit=limit,
        eviction_mode=eviction_mode,
    )
    return pack.text, pack.skill_ids


def build_skill_context(
    workspace_root: str | Path,
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    content: str | None = None,
    limit: int = DEFAULT_SKILL_LIMIT,
    eviction_mode: EvictionMode | str = EvictionMode.OFF,
) -> str:
    """Read, select, and format skills for the given terrain.

    Always returns a string (possibly empty). Never propagates exceptions —
    returns "" on any failure.
    """
    return build_skill_pack(
        workspace_root,
        model=model,
        task_kind=task_kind,
        target_files=target_files,
        content=content,
        limit=limit,
        eviction_mode=eviction_mode,
    ).text
