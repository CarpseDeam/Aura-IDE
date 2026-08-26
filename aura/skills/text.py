from __future__ import annotations

import logging
from pathlib import Path

from aura.skills.eviction import (
    EvictionMode,
    apply_eviction_mode,
    compute_eviction_verdicts,
)
from aura.skills.explicit_selection import (
    explicit_candidate,
    format_explicit_skill_entry,
    format_explicit_skills,
    resolve_explicit_skills,
)
from aura.skills.models import (
    Skill,
    SkillProvenance,
    compute_skill_id,
    skill_body_hash,
    skill_label,
)
from aura.skills.pack_models import (
    EXPLICIT_CANDIDATE_CONFLICT,
    EXPLICIT_MALFORMED,
    EXPLICIT_UNAVAILABLE,
    SkillCandidate,
    SkillPack,
    SkillRecord,
    UnresolvedSkillReference,
)
from aura.skills.reader import read_skills
from aura.skills.selection import (
    DEFAULT_SKILL_LIMIT,
    has_terrain_signal,
    score_skills,
)

__all__ = [
    "EXPLICIT_CANDIDATE_CONFLICT",
    "EXPLICIT_MALFORMED",
    "EXPLICIT_UNAVAILABLE",
    "SkillCandidate",
    "SkillPack",
    "SkillRecord",
    "UnresolvedSkillReference",
    "build_skill_context",
    "build_skill_context_with_ids",
    "build_skill_pack",
    "format_explicit_skill_entry",
    "format_skill_index",
    "format_skills",
]

logger = logging.getLogger(__name__)

_MAX_REPORTED_SKIPPED = 20

# Provenance precedence order for the frozen candidate snapshot, so the index
# and ``load_skills`` echo workspace-authored, then bundled, then graduated,
# then refined — deterministic and score-stable within each group.
_CANDIDATE_PRECEDENCE: dict[SkillProvenance, int] = {
    SkillProvenance.USER_AUTHORED: 0,
    SkillProvenance.BUNDLED: 1,
    SkillProvenance.FAILURE_GRADUATED: 2,
    SkillProvenance.REFLECTION_REFINED: 3,
}


_SKILL_LOADING_GUIDANCE = (
    "Selected specialty skills for this request. Their full bodies are not "
    "preloaded. Load the body of a skill only when the requested work "
    "materially needs its detailed procedure, and batch related activations "
    "into one load_skills call."
)

_SKILL_INDEX_HEADER = "### Skills"

# Graduated and refined guards stay eager because they encode a hazard the
# runtime has actually hit, and a hazard warning that arrives only on request
# arrives too late. That earns a short warning, not a procedure: past this cap
# a guard is coaching, and coaching belongs behind ``load_skills`` like every
# other body.
_EAGER_GUARD_CHAR_CAP = 500


def eager_guard_text(skill: Skill) -> str:
    """Return the bounded guard text injected eagerly for *skill*.

    Bodies within the cap are injected whole. Longer ones are cut at a line
    boundary and point at ``load_skills`` for the rest, so nothing is lost.
    """
    text = skill.text.strip()
    if len(text) <= _EAGER_GUARD_CHAR_CAP:
        return text
    head = text[:_EAGER_GUARD_CHAR_CAP]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    return (
        head.rstrip()
        + f"\n[guard truncated — load_skills {compute_skill_id(skill)} for the full body]"
    )


def format_skills(skills: list[Skill], limit: int = DEFAULT_SKILL_LIMIT) -> str:
    """Compatibility formatter: full skill bodies, sections by precedence.

    The production prompt no longer uses this — ``build_skill_pack`` emits a
    compact index instead (see :func:`format_skill_index`).  This wrapper keeps
    the historical full-body semantics for any remaining caller that genuinely
    wants selected bodies inline, and is explicitly *not* the initial-context
    representation.
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


def _format_index_entry(skill: Skill, reason: str) -> str:
    """One compact, deterministic index line for an authored/bundled skill.

    Carries exactly the required surface: stable id, human label, one-line
    description, provenance, deterministic selection reason, and whether the
    skill directory has supporting resources.  No trigger, path-glob, or
    workspace-marker lists and no raw origin metadata ever reach this line.
    """
    resources = "present" if skill.has_resources else "none"
    description = skill.description or "no description"
    return (
        f"- {skill_label(skill)} ({compute_skill_id(skill)}) — "
        f"{description} [{skill.provenance.value}] "
        f"reason: {reason}; resources: {resources}"
    )


def format_skill_index(
    chosen: list[Skill],
    reasons_by_id: dict[str, str],
) -> tuple[str, int, int]:
    """Format the initial skill context: index + eager guards.

    Returns ``(text, index_chars, guard_chars)``.  ``index_chars`` counts the
    compact metadata lines for authored/bundled candidates; ``guard_chars``
    counts eagerly injected graduated/refined guard text.  Precedence is
    preserved: workspace-authored, then bundled, then graduated, then refined.
    """
    if not chosen:
        return "", 0, 0

    authored = [s for s in chosen if s.provenance == SkillProvenance.USER_AUTHORED]
    bundled = [s for s in chosen if s.provenance == SkillProvenance.BUNDLED]
    graduated = [s for s in chosen if s.provenance == SkillProvenance.FAILURE_GRADUATED]
    refined = [s for s in chosen if s.provenance == SkillProvenance.REFLECTION_REFINED]

    index_lines: list[str] = [_SKILL_INDEX_HEADER, _SKILL_LOADING_GUIDANCE]
    index_skills = authored + bundled
    for skill in index_skills:
        index_lines.append(
            _format_index_entry(skill, reasons_by_id.get(compute_skill_id(skill), "terrain match"))
        )
    index_text = "\n".join(index_lines)
    index_chars = len(index_text)

    guard_lines: list[str] = []
    if graduated:
        guard_lines.append("### Learned Hazard Guards")
        guard_lines.extend(eager_guard_text(s) for s in graduated)
    if refined:
        guard_lines.append("### Refined Skill Guards")
        guard_lines.extend(eager_guard_text(s) for s in refined)
    guard_text = "\n".join(guard_lines)
    guard_chars = len(guard_text)

    parts = [index_text, guard_text]
    return "\n\n".join(part for part in parts if part), index_chars, guard_chars


def build_skill_pack(
    workspace_root: str | Path,
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    content: str | None = None,
    limit: int = DEFAULT_SKILL_LIMIT,
    eviction_mode: EvictionMode | str = EvictionMode.OFF,
    explicit_install_ids: tuple[str, ...] = (),
) -> SkillPack:
    """Select this turn's skills and report why each was indexed or skipped.

    Two selection paths compose here, and this is the one place they compose.
    Explicitly selected skills come from ``explicit_install_ids`` — stable
    ``scope:name`` identities the user named — and are included with their full
    bodies, in the supplied order, whether or not the turn has any terrain.
    Automatic selection stays terrain-driven and unchanged: with no terrain
    signal at all nothing is selected automatically, so a workspace-startup
    composition never dumps an unrelated pack into the prompt.

    Explicit selections do not spend the automatic limit — they are removed
    from the automatic pool before it is capped — so a turn still receives up
    to ``limit`` automatic candidates alongside them, and a skill that is both
    explicitly selected and automatically relevant appears exactly once.

    The returned pack's ``text`` is the *initial* context: explicit full
    bodies, then a compact index for automatic authored/bundled candidates,
    then eager graduated/refined guard text.  An explicit reference that
    resolves to nothing is reported in ``unresolved_explicit`` rather than
    raised — this never propagates exceptions and returns an empty pack.
    """
    try:
        skills = read_skills(workspace_root)
        explicit, unresolved = resolve_explicit_skills(skills, explicit_install_ids)
        if not skills:
            return SkillPack(unresolved_explicit=tuple(unresolved))

        explicit_ids = {compute_skill_id(skill) for _reference, skill in explicit}
        explicit_text, explicit_chars = format_explicit_skills(explicit)
        explicit_candidates = [
            explicit_candidate(install_id, skill) for install_id, skill in explicit
        ]

        if not has_terrain_signal(
            model=model,
            task_kind=task_kind,
            target_files=target_files,
            content=content,
        ):
            skipped_without_terrain = [
                _record(skill, "no turn terrain to select against", 0)
                for skill in skills
                if compute_skill_id(skill) not in explicit_ids
            ]
            return SkillPack(
                text=explicit_text,
                explicit_chars=explicit_chars,
                candidates=tuple(explicit_candidates),
                skipped=tuple(skipped_without_terrain[:_MAX_REPORTED_SKIPPED]),
                unresolved_explicit=tuple(unresolved),
            )

        ranked = score_skills(
            skills,
            model=model,
            task_kind=task_kind,
            target_files=target_files,
            content=content,
        )
        relevant = [item for item in ranked if item.is_relevant]
        reasons_by_id = {
            compute_skill_id(item.skill): (", ".join(item.reasons) or "terrain match")
            for item in relevant
        }
        # Explicit selections leave the automatic pool *before* the limit is
        # applied, so they cost the turn none of its automatic allowance.
        chosen = [
            skill
            for skill in _dedupe_skills([item.skill for item in relevant])
            if compute_skill_id(skill) not in explicit_ids
        ][:limit]

        mode = EvictionMode.from_value(eviction_mode)
        if mode != EvictionMode.OFF:
            verdicts = compute_eviction_verdicts(
                Path(workspace_root),
                task_kind=task_kind,
            )
            chosen = apply_eviction_mode(chosen, verdicts, mode=mode)

        index_text, index_chars, guard_chars = format_skill_index(chosen, reasons_by_id)
        text = "\n\n".join(part for part in (explicit_text, index_text) if part)
        chosen_ids = {compute_skill_id(skill) for skill in chosen}

        candidates: list[SkillCandidate] = []
        skipped: list[SkillRecord] = []
        for item in ranked:
            skill = item.skill
            skill_id = compute_skill_id(skill)
            if skill_id in explicit_ids:
                # Already a candidate through the explicit selection — never
                # indexed twice, never reported as skipped.
                continue
            if skill_id in chosen_ids and not any(
                candidate.skill_id == skill_id for candidate in candidates
            ):
                eager_guard = skill.provenance in (
                    SkillProvenance.FAILURE_GRADUATED,
                    SkillProvenance.REFLECTION_REFINED,
                )
                reason = reasons_by_id.get(skill_id, "terrain match")
                candidates.append(
                    SkillCandidate(
                        skill_id=skill_id,
                        label=skill_label(skill),
                        provenance=skill.provenance.value,
                        description=skill.description or "",
                        reason=reason,
                        index_chars=(
                            0
                            if eager_guard
                            else _index_entry_chars(skill, skill_id, reason)
                        ),
                        guard_chars=(
                            len(eager_guard_text(skill)) if eager_guard else 0
                        ),
                        body_chars=len(skill.text),
                        body_hash=skill_body_hash(skill),
                        has_resources=skill.has_resources,
                        eager_guard=eager_guard,
                        skill=skill,
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
            skipped.append(_record(skill, reason, 0))

        candidates.sort(
            key=lambda c: _CANDIDATE_PRECEDENCE.get(SkillProvenance(c.provenance), 4)
        )
        return SkillPack(
            text=text,
            index_chars=index_chars,
            guard_chars=guard_chars,
            explicit_chars=explicit_chars,
            candidates=tuple(explicit_candidates + candidates),
            skipped=tuple(skipped[:_MAX_REPORTED_SKIPPED]),
            unresolved_explicit=tuple(unresolved),
        )
    except Exception:
        logger.debug("build_skill_pack failed", exc_info=True)
        return SkillPack()


def _index_entry_chars(skill: Skill, skill_id: str, reason: str) -> int:
    return len(_format_index_entry(skill, reason))


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
    """Read, select, and index skills for the given terrain.

    Returns the initial skill context: a compact deterministic index of the
    selected authored/bundled candidates plus eagerly injected graduated/
    refined guard text.  Full authored/bundled bodies are intentionally absent
    here and load through the dedicated ``load_skills`` tool.

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
