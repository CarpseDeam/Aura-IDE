from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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


def compute_skill_id(skill_text: str) -> str:
    """Deterministic stable source ID for a skill based on its text.

    Returns a "skill_"-prefixed hex string derived from SHA-256[:16].
    """
    import hashlib

    suffix = hashlib.sha256(skill_text.encode("utf-8")).hexdigest()[:16]
    return f"skill_{suffix}"
