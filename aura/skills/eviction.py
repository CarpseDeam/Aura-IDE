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
from enum import Enum
from pathlib import Path
from typing import Any

from aura.skills.models import Skill, SkillProvenance, compute_skill_id
from aura.skills.reader import read_skills
from aura.skills.utility import derive_source_utility

logger = logging.getLogger(__name__)


class EvictionMode(str, Enum):
    OFF = "off"
    DRY_RUN = "dry_run"
    ENFORCE = "enforce"

    @classmethod
    def from_value(cls, value: "EvictionMode | str") -> "EvictionMode":
        if isinstance(value, cls):
            return value
        return cls(str(value))


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
    task_kind: str | None = None,
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
        current_task_kind = _normalize_task_kind(task_kind)

        verdicts: list[EvictionVerdict] = []
        for skill in skills:
            skill_id = compute_skill_id(skill)
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

            utility_task_kind = _utility_field(source_util, "task_kind")
            utility_status = _utility_field(source_util, "status")
            utility_lift = _utility_field(source_util, "lift")
            loaded_n = int(_utility_field(source_util, "loaded_n") or 0)
            not_loaded_n = int(_utility_field(source_util, "not_loaded_n") or 0)
            utility_task_kind_text = (
                str(utility_task_kind) if utility_task_kind is not None else None
            )
            utility_lift_float = (
                None if utility_lift is None else float(utility_lift)
            )

            if _normalize_task_kind(utility_task_kind_text) != current_task_kind:
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=False,
                    reason=f"different terrain: current={task_kind!r}, "
                           f"utility={utility_task_kind_text!r}",
                    lift=utility_lift_float,
                    loaded_n=loaded_n,
                    not_loaded_n=not_loaded_n,
                    task_kind=utility_task_kind_text,
                ))
                continue

            if utility_status == "insufficient":
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=False,
                    reason=f"insufficient data: loaded_n={loaded_n}, "
                           f"not_loaded_n={not_loaded_n}, "
                           f"need >= {min_arm} each",
                    lift=utility_lift_float,
                    loaded_n=loaded_n,
                    not_loaded_n=not_loaded_n,
                    task_kind=utility_task_kind_text,
                ))
                continue

            if utility_status != "measured":
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=False,
                    reason=f"utility status {utility_status!r} is not measured",
                    lift=utility_lift_float,
                    loaded_n=loaded_n,
                    not_loaded_n=not_loaded_n,
                    task_kind=utility_task_kind_text,
                ))
                continue

            lift = utility_lift_float
            if lift is not None and lift < negative_lift_threshold:
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=True,
                    reason=f"negative lift {lift:+.3f} on terrain "
                           f"'{utility_task_kind_text}'",
                    lift=lift,
                    loaded_n=loaded_n,
                    not_loaded_n=not_loaded_n,
                    task_kind=utility_task_kind_text,
                ))
            else:
                lift_text = "N/A" if lift is None else f"{lift:+.3f}"
                verdicts.append(EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix=prefix,
                    provenance=skill.provenance,
                    would_evict=False,
                    reason=f"lift {lift_text} >= threshold on terrain "
                           f"'{utility_task_kind_text}'",
                    lift=lift,
                    loaded_n=loaded_n,
                    not_loaded_n=not_loaded_n,
                    task_kind=utility_task_kind_text,
                ))

        return verdicts
    except Exception:
        logger.exception("compute_eviction_verdicts failed (degrading to empty)")
        return []


def apply_eviction_mode(
    skills: list[Skill],
    verdicts: list[EvictionVerdict],
    *,
    mode: EvictionMode | str = EvictionMode.OFF,
) -> list[Skill]:
    """Return skills after applying an explicit eviction mode.

    OFF and DRY_RUN are observational and never filter. ENFORCE filters only
    skills with a computed would_evict verdict.
    """
    eviction_mode = EvictionMode.from_value(mode)
    if eviction_mode != EvictionMode.ENFORCE:
        return list(skills)

    evicted_ids = {verdict.skill_id for verdict in verdicts if verdict.would_evict}
    if not evicted_ids:
        return list(skills)
    return [skill for skill in skills if compute_skill_id(skill) not in evicted_ids]


def _utility_field(utility: Any, field: str) -> Any:
    if isinstance(utility, dict):
        return utility.get(field)
    return getattr(utility, field, None)


def _normalize_task_kind(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("-", "_").split())


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
