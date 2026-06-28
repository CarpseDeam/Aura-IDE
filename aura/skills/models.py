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
