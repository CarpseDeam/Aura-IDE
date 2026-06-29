from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class SkillProvenance(str, Enum):
    BUNDLED = "bundled"
    USER_AUTHORED = "user_authored"
    FAILURE_GRADUATED = "failure_graduated"
    REFLECTION_REFINED = "reflection_refined"


@dataclass(frozen=True)
class Skill:
    text: str
    task_kinds: tuple[str, ...]
    path_globs: tuple[str, ...]
    model: str | None
    provenance: SkillProvenance
    origin: tuple[tuple[str, str], ...]
    triggers: tuple[str, ...] = ()


def compute_skill_id(
    skill: Skill | str,
    provenance: SkillProvenance | str | None = None,
) -> str:
    """Deterministic stable source ID for a skill.

    Internal callers should pass a Skill so provenance and text both
    participate. Passing plain text remains supported for compatibility.
    """
    import hashlib

    if isinstance(skill, Skill):
        skill_text = skill.text
        provenance_value = skill.provenance.value
    else:
        skill_text = str(skill)
        provenance_value = _normalize_provenance_value(provenance)
    payload = f"{provenance_value}\0{skill_text}"
    suffix = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"skill_{suffix}"


def _normalize_provenance_value(provenance: Any) -> str:
    if isinstance(provenance, SkillProvenance):
        return provenance.value
    if provenance is None:
        return "unknown"
    return str(provenance)
