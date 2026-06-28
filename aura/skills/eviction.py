"""Phase 4 — Eviction: dry-run computation of which derived skills would
be withheld based on same-terrain measured utility lift.

Eviction is *derived, never destructive*: it is a recomputed selection
state, "do not load this right now," not a delete, tombstone, or mutation.
Raw outcome rows persist. Refined and graduated artifacts persist.
If the terrain or lift changes, the skill can naturally derive back into
selection.

Sticky provenance: bundled and user-authored skills are never auto-evicted.
Only failure-graduated and reflection-refined skills must earn their slot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aura.skills.models import SkillProvenance, compute_skill_id
from aura.skills.reader import read_skills
from aura.skills.utility import derive_source_utility

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvictionVerdict:
    """Dry-run eviction verdict for one skill."""

    skill_id: str
    skill_text_prefix: str
    provenance: SkillProvenance
    would_evict: bool
    reason: str
    lift: float | None
    loaded_n: int
    not_loaded_n: int
    task_kind: str | None


def compute_eviction_verdicts(
    workspace_root: Path,
    *,
    min_arm: int = 3,
    negative_lift_threshold: float = 0.0,
) -> list[EvictionVerdict]:
    """Compute dry-run eviction verdicts for all skills.

    BUNDLED and USER_AUTHORED skills are never auto-evicted (sticky
    provenance).  Only FAILURE_GRADUATED and REFLECTION_REFINED skills
    must earn their slot based on same-terrain utility lift.

    Silently degrades to [] on any exception.
    """
    try:
        skills = read_skills(workspace_root)
        utility = derive_source_utility(workspace_root, min_arm=min_arm)

        verdicts: list[EvictionVerdict] = []
        for skill in skills:
            skill_id = compute_skill_id(skill.text)
            prefix = skill.text.split("\n")[0] if skill.text else ""
            if len(prefix) > 80:
                prefix = prefix[:80] + "..."

            # Sticky provenance: never evict bundled or user-authored
            if skill.provenance in (SkillProvenance.BUNDLED, SkillProvenance.USER_AUTHORED):
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=False,
                    reason="sticky provenance",
                    lift=None,
                    loaded_n=0,
                    not_loaded_n=0,
                    task_kind=None,
                ))
                continue

            # Check utility data for this skill's source_id
            source_util = utility.get(skill_id)
            if source_util is None:
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=False,
                    reason="no utility data yet",
                    lift=None,
                    loaded_n=0,
                    not_loaded_n=0,
                    task_kind=None,
                ))
                continue

            if source_util.status == "insufficient":
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=False,
                    reason=f"insufficient data: loaded_n={source_util.loaded_n}, "
                           f"not_loaded_n={source_util.not_loaded_n}, "
                           f"need >= {min_arm} each",
                    lift=source_util.lift,
                    loaded_n=source_util.loaded_n,
                    not_loaded_n=source_util.not_loaded_n,
                    task_kind=source_util.task_kind,
                ))
                continue

            # source_util.status == "measured"
            lift = source_util.lift
            if lift is not None and lift < negative_lift_threshold:
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=True,
                    reason=f"negative lift {lift:+.3f} on terrain '{source_util.task_kind}'",
                    lift=lift,
                    loaded_n=source_util.loaded_n,
                    not_loaded_n=source_util.not_loaded_n,
                    task_kind=source_util.task_kind,
                ))
            else:
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=False,
                    reason=f"lift {lift:+.3f} >= threshold on terrain "
                           f"'{source_util.task_kind}'",
                    lift=lift,
                    loaded_n=source_util.loaded_n,
                    not_loaded_n=source_util.not_loaded_n,
                    task_kind=source_util.task_kind,
                ))

        return verdicts
    except Exception:
        logger.exception("compute_eviction_verdicts failed (degrading to empty)")
        return []


def format_eviction_report(verdicts: list[EvictionVerdict]) -> str:
    """Return a human-readable eviction report string."""
    if not verdicts:
        return "No skills to evaluate."

    total = len(verdicts)
    evicted = [v for v in verdicts if v.would_evict]
    retained = [v for v in verdicts if not v.would_evict]

    lines: list[str] = [
        "Phase 4A — Eviction Report (dry-run only)",
        f"Total skills evaluated: {total}",
        f"Would evict: {len(evicted)}",
        f"Would retain: {len(retained)}",
        "",
    ]

    if evicted:
        lines.append("--- Evicted Skills ---")
        for v in evicted:
            lines.append(f"  {v.skill_id}")
            lines.append(f"    provenace: {v.provenance.value}")
            lines.append(f"    reason: {v.reason}")
            lines.append(f"    lift: {v.lift:+.3f}" if v.lift is not None else "    lift: N/A")
            if v.task_kind:
                lines.append(f"    terrain: {v.task_kind}")
        lines.append("")

    if retained:
        # Group by provenance
        by_provenance: dict[str, list[EvictionVerdict]] = {}
        for v in retained:
            key = v.provenance.value
            by_provenance.setdefault(key, []).append(v)

        lines.append("--- Retained Skills ---")
        for prov in sorted(by_provenance):
            group = by_provenance[prov]
            lines.append(f"  [{prov}] ({len(group)} skills)")
            for v in group:
                lines.append(f"    {v.skill_id}")
                lines.append(f"      reason: {v.reason}")
        lines.append("")

    lines.append("(dry-run only — no state was modified)")
    return "\n".join(lines)


def summarize_eviction_report(verdicts: list[EvictionVerdict]) -> dict[str, Any]:
    """Return a machine-readable eviction report dict."""
    evicted = [v for v in verdicts if v.would_evict]
    return {
        "total_skills": len(verdicts),
        "would_evict_count": len(evicted),
        "would_evict": [
            {
                "skill_id": v.skill_id,
                "provenance": v.provenance.value,
                "reason": v.reason,
                "lift": v.lift,
                "task_kind": v.task_kind,
            }
            for v in evicted
        ],
        "dry_run": True,
    }
