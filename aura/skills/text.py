from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from aura.skills.eviction import (
    EvictionMode,
    apply_eviction_mode,
    compute_eviction_verdicts,
)
from aura.skills.identity import InstalledSkillId
from aura.skills.models import (
    Skill,
    SkillProvenance,
    compute_skill_id,
    skill_body_hash,
    skill_label,
)
from aura.skills.reader import read_skills
from aura.skills.selection import (
    DEFAULT_SKILL_LIMIT,
    has_terrain_signal,
    score_skills,
)

logger = logging.getLogger(__name__)

_MAX_REPORTED_SKIPPED = 20

#: Why an explicitly requested installed skill could not be activated.
EXPLICIT_MALFORMED = "malformed"
EXPLICIT_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class UnresolvedSkillReference:
    """One explicitly requested installed skill that could not be activated.

    Reported instead of being dropped, so the send layer can refuse the turn
    locally rather than let a user-selected skill vanish silently.
    """

    reference: str
    status: str
    reason: str


@dataclass(frozen=True)
class SkillRecord:
    """One inspectable decision about a single skill for this turn."""

    skill_id: str
    label: str
    provenance: str
    reason: str
    char_count: int


@dataclass(frozen=True)
class SkillCandidate:
    """One selected skill of the frozen per-turn candidate index.

    Carries everything the dedicated ``load_skills`` tool needs to resolve an
    activation against the *frozen* snapshot: the stable id, a one-line
    description, the deterministic selection reason, body metrics, and the full
    ``Skill`` (so a body is served from the snapshot, never a re-scan).
    """

    skill_id: str
    label: str
    provenance: str
    description: str
    reason: str
    index_chars: int
    body_chars: int
    body_hash: str
    has_resources: bool
    eager_guard: bool
    skill: Skill
    #: Characters this candidate actually contributes to the initial prompt as
    #: an eager guard — the bounded excerpt, not the whole body. Zero for
    #: authored/bundled candidates, which contribute an index line instead.
    guard_chars: int = 0
    #: True when the user named this skill for the turn rather than terrain
    #: selecting it. Explicit candidates carry their full body in the initial
    #: context and begin the frozen turn already active.
    explicit: bool = False
    #: The stable ``scope:name`` identity the explicit selection named. Empty
    #: for automatically selected candidates, which have no user-facing
    #: reference — and always distinct from ``skill_id``, which stays the
    #: content-addressed per-turn candidate id.
    install_id: str = ""
    #: Characters this candidate contributes to the explicitly-selected
    #: section — its heading line plus its full body. Zero for automatic
    #: candidates.
    explicit_chars: int = 0


@dataclass(frozen=True)
class SkillPack:
    """This turn's frozen skill selection plus why each was indexed or skipped.

    ``text`` is the *initial* skill context block: the full bodies of the
    explicitly selected skills, then a compact deterministic index for
    automatically selected authored/bundled candidates plus eagerly injected
    guard text for graduated/refined candidates.  Full *automatic*
    authored/bundled bodies are deliberately absent here — they become
    available only through ``load_skills``.
    """

    text: str = ""
    index_chars: int = 0
    guard_chars: int = 0
    #: Characters the explicitly-selected full-body section contributes.
    explicit_chars: int = 0
    candidates: tuple[SkillCandidate, ...] = field(default_factory=tuple)
    skipped: tuple[SkillRecord, ...] = field(default_factory=tuple)
    #: Explicit references that named no activatable installed skill, in
    #: supplied order.  Never raised — reported, so a caller can refuse.
    unresolved_explicit: tuple[UnresolvedSkillReference, ...] = field(
        default_factory=tuple
    )

    @property
    def skill_ids(self) -> list[str]:
        return [candidate.skill_id for candidate in self.candidates]

    @property
    def explicit_candidates(self) -> tuple[SkillCandidate, ...]:
        """Explicitly selected candidates, in the supplied selection order."""
        return tuple(c for c in self.candidates if c.explicit)

    @property
    def selected(self) -> tuple[SkillRecord, ...]:
        """Compatibility projection of candidates onto the old record shape.

        Kept so older inspection callers that only need ids/labels/reasons keep
        working; production ledger rendering uses ``candidates`` directly.
        """
        return tuple(
            SkillRecord(
                skill_id=candidate.skill_id,
                label=candidate.label,
                provenance=candidate.provenance,
                reason=candidate.reason,
                char_count=_candidate_prompt_chars(candidate),
            )
            for candidate in self.candidates
        )


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

_EXPLICIT_SKILL_HEADER = "### Explicitly Selected Skills"

_EXPLICIT_SKILL_GUIDANCE = (
    "These skills were explicitly selected for this request and are already "
    "active — their full instructions follow, so load_skills is neither "
    "needed nor useful for them. Follow them for this request."
)

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


def format_explicit_skill_entry(skill: Skill, install_id: str) -> str:
    """One explicitly selected skill's full-body entry.

    Unlike the automatic index line, this carries the whole body: the user
    asked for this skill by its stable installed identity, so withholding the
    procedure behind ``load_skills`` would just cost a round trip.
    """
    description = skill.description or "no description"
    return (
        f"#### {skill_label(skill)} ({install_id}) — {description}\n"
        f"{skill.text.strip()}"
    )


def format_explicit_skills(resolved: list[tuple[str, Skill]]) -> tuple[str, int]:
    """Format the explicitly-selected section, in the supplied order.

    Returns ``(text, explicit_chars)``.  Empty selection yields ``("", 0)``,
    so a turn with no explicit skills composes exactly as it always has.
    """
    if not resolved:
        return "", 0
    parts = [_EXPLICIT_SKILL_HEADER, _EXPLICIT_SKILL_GUIDANCE]
    parts.extend(
        format_explicit_skill_entry(skill, install_id)
        for install_id, skill in resolved
    )
    text = "\n\n".join(parts)
    return text, len(text)


def _candidate_prompt_chars(candidate: SkillCandidate) -> int:
    """Characters *this* candidate contributes to the initial skill context."""
    if candidate.explicit:
        return candidate.explicit_chars
    if candidate.eager_guard:
        return candidate.guard_chars
    return candidate.index_chars


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


def _resolve_explicit_skills(
    skills: list[Skill],
    references: tuple[str, ...],
) -> tuple[list[tuple[str, Skill]], list[UnresolvedSkillReference]]:
    """Resolve stable ``scope:name`` references against the effective skills.

    Resolution reads nothing of its own: the candidates are exactly what the
    runtime reader already returned, so a disabled, invalid, shadowed, or
    workspace-inapplicable installed skill is simply absent and therefore
    unresolvable.  Supplied order is preserved, surrounding whitespace is
    normalized, repeated references collapse to their first occurrence, and
    anything that resolves to nothing is reported rather than dropped.
    """
    by_install_id = {skill.install_id: skill for skill in skills if skill.install_id}
    resolved: list[tuple[str, Skill]] = []
    unresolved: list[UnresolvedSkillReference] = []
    seen_references: set[str] = set()
    seen_candidates: set[str] = set()

    for raw in references:
        reference = raw.strip() if isinstance(raw, str) else ""
        parsed = InstalledSkillId.parse(reference)
        key = str(parsed) if parsed is not None else reference.casefold()
        if key in seen_references:
            continue
        seen_references.add(key)

        if parsed is None:
            unresolved.append(
                UnresolvedSkillReference(
                    reference=reference,
                    status=EXPLICIT_MALFORMED,
                    reason="not a valid scope:name installed skill reference",
                )
            )
            continue

        installed_id = str(parsed)
        skill = by_install_id.get(installed_id)
        if skill is None:
            unresolved.append(
                UnresolvedSkillReference(
                    reference=installed_id,
                    status=EXPLICIT_UNAVAILABLE,
                    reason=(
                        "no enabled installed skill with this identity is "
                        "available in this workspace"
                    ),
                )
            )
            continue

        # Two installed skills can share a body, and therefore a candidate id.
        # The first reference owns the candidate; the second is already active
        # through it, so it is neither duplicated nor reported unresolved.
        candidate_id = compute_skill_id(skill)
        if candidate_id in seen_candidates:
            continue
        seen_candidates.add(candidate_id)
        resolved.append((installed_id, skill))

    return resolved, unresolved


def _explicit_candidate(install_id: str, skill: Skill) -> SkillCandidate:
    """Build one frozen candidate for an explicitly selected installed skill.

    Keeps the content-derived candidate id and body hash every candidate
    carries, and adds the explicit metadata and character accounting the
    Context Gearbox ledger needs to describe the selection truthfully.
    """
    return SkillCandidate(
        skill_id=compute_skill_id(skill),
        label=skill_label(skill),
        provenance=skill.provenance.value,
        description=skill.description or "",
        reason=f"explicitly selected by the user ({install_id})",
        index_chars=0,
        body_chars=len(skill.text),
        body_hash=skill_body_hash(skill),
        has_resources=skill.has_resources,
        eager_guard=False,
        skill=skill,
        explicit=True,
        install_id=install_id,
        explicit_chars=len(format_explicit_skill_entry(skill, install_id)),
    )


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
        explicit, unresolved = _resolve_explicit_skills(skills, explicit_install_ids)
        if not skills:
            return SkillPack(unresolved_explicit=tuple(unresolved))

        explicit_ids = {compute_skill_id(skill) for _reference, skill in explicit}
        explicit_text, explicit_chars = format_explicit_skills(explicit)
        explicit_candidates = [
            _explicit_candidate(install_id, skill) for install_id, skill in explicit
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
