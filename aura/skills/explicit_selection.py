"""Explicit installed-skill resolution and prompt-entry construction."""
from __future__ import annotations

from aura.skills.identity import InstalledSkillId
from aura.skills.models import Skill, compute_skill_id, skill_body_hash, skill_label
from aura.skills.pack_models import (
    EXPLICIT_CANDIDATE_CONFLICT,
    EXPLICIT_MALFORMED,
    EXPLICIT_UNAVAILABLE,
    SkillCandidate,
    UnresolvedSkillReference,
)

EXPLICIT_SKILL_HEADER = "### Explicitly Selected Skills"

_EXPLICIT_SKILL_GUIDANCE = (
    "These skills were explicitly selected for this request and are already "
    "active — their full instructions follow, so load_skills is neither "
    "needed nor useful for them. Supporting resources marked present can be "
    "read immediately with read_skill_resource and the displayed skill_id. "
    "Follow these skills for this request."
)


def resolve_explicit_skills(
    skills: list[Skill],
    references: tuple[str, ...],
) -> tuple[list[tuple[str, Skill]], list[UnresolvedSkillReference]]:
    """Resolve stable ``scope:name`` references against the effective set.

    Repeated occurrences of the same normalized installed identity collapse.
    If a different identity has the same content-derived candidate id, the
    first reference deterministically owns that id and its frozen resource
    directory; every later identity is reported as a structured conflict.
    """
    by_install_id = {skill.install_id: skill for skill in skills if skill.install_id}
    resolved: list[tuple[str, Skill]] = []
    unresolved: list[UnresolvedSkillReference] = []
    seen_references: set[str] = set()
    candidate_owners: dict[str, str] = {}

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

        candidate_id = compute_skill_id(skill)
        existing_owner = candidate_owners.get(candidate_id)
        if existing_owner is not None:
            unresolved.append(
                UnresolvedSkillReference(
                    reference=installed_id,
                    status=EXPLICIT_CANDIDATE_CONFLICT,
                    reason=(
                        f"content-derived candidate id {candidate_id} is already "
                        f"owned by explicit skill {existing_owner}; {installed_id} "
                        "may have a different supporting-resource directory"
                    ),
                )
            )
            continue

        candidate_owners[candidate_id] = installed_id
        resolved.append((installed_id, skill))

    return resolved, unresolved


def format_explicit_skill_entry(skill: Skill, install_id: str) -> str:
    """Format a full-body entry with both identities and resource status."""
    skill_id = compute_skill_id(skill)
    resources = "present" if skill.has_resources else "none"
    description = skill.description or "no description"
    return (
        f"#### {skill_label(skill)} — {description}\n"
        f"Installed identity: {install_id}\n"
        f"Candidate skill_id: {skill_id}\n"
        f"Supporting resources: {resources}\n"
        f"{skill.text.strip()}"
    )


def format_explicit_skills(resolved: list[tuple[str, Skill]]) -> tuple[str, int]:
    """Format the explicit section and return its exact character count."""
    if not resolved:
        return "", 0
    parts = [EXPLICIT_SKILL_HEADER, _EXPLICIT_SKILL_GUIDANCE]
    parts.extend(
        format_explicit_skill_entry(skill, install_id)
        for install_id, skill in resolved
    )
    text = "\n\n".join(parts)
    return text, len(text)


def explicit_candidate(install_id: str, skill: Skill) -> SkillCandidate:
    """Build one frozen explicitly selected installed-skill candidate."""
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
