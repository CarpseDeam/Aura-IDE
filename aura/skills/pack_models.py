"""Frozen per-turn skill-pack models.

These data structures describe the output of skill selection without owning
discovery, selection, prompt formatting, or turn activation behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aura.skills.models import Skill

#: Why an explicitly requested installed skill could not be activated.
EXPLICIT_MALFORMED = "malformed"
EXPLICIT_UNAVAILABLE = "unavailable"
EXPLICIT_CANDIDATE_CONFLICT = "candidate_id_conflict"


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

    Carries everything the dedicated ``load_skills`` and
    ``read_skill_resource`` tools need to resolve against the *frozen*
    snapshot, including the full ``Skill`` and its source directory.
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
    #: an eager guard — the bounded excerpt, not the whole body.
    guard_chars: int = 0
    #: True when the user named this skill for the turn. Explicit candidates
    #: carry their full body in the initial context and begin already active.
    explicit: bool = False
    #: Stable ``scope:name`` identity for an explicit candidate. This remains
    #: distinct from the content-addressed per-turn ``skill_id``.
    install_id: str = ""
    #: Exact characters this candidate contributes through its explicit entry.
    explicit_chars: int = 0

    @property
    def prompt_chars(self) -> int:
        """Characters this candidate contributes to initial skill context."""
        if self.explicit:
            return self.explicit_chars
        if self.eager_guard:
            return self.guard_chars
        return self.index_chars


@dataclass(frozen=True)
class SkillPack:
    """This turn's frozen skill selection plus composition accounting."""

    text: str = ""
    index_chars: int = 0
    guard_chars: int = 0
    explicit_chars: int = 0
    candidates: tuple[SkillCandidate, ...] = field(default_factory=tuple)
    skipped: tuple[SkillRecord, ...] = field(default_factory=tuple)
    #: Explicit references that could not safely become active, in supplied
    #: order. Never raised — reported, so a caller can refuse locally.
    unresolved_explicit: tuple[UnresolvedSkillReference, ...] = field(
        default_factory=tuple
    )

    @property
    def skill_ids(self) -> list[str]:
        return [candidate.skill_id for candidate in self.candidates]

    @property
    def explicit_candidates(self) -> tuple[SkillCandidate, ...]:
        """Explicitly selected candidates, in supplied selection order."""
        return tuple(candidate for candidate in self.candidates if candidate.explicit)

    @property
    def selected(self) -> tuple[SkillRecord, ...]:
        """Compatibility projection of candidates onto the old record shape."""
        return tuple(
            SkillRecord(
                skill_id=candidate.skill_id,
                label=candidate.label,
                provenance=candidate.provenance,
                reason=candidate.reason,
                char_count=candidate.prompt_chars,
            )
            for candidate in self.candidates
        )
