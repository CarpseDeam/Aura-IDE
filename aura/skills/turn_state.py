"""Turn-scoped frozen skill candidates and the dedicated load_skills resolver.

One instance is created when a real user turn begins (owned by the send loop's
``_SendState``) and is *never* recomputed per model/tool round.  The ``load_skills``
tool resolves only against this frozen snapshot — the same deterministic
selection that produced the initial skill index — so a body is served from the
turn's own candidates, never from a freshly scanned library, and unrelated
global skills are outside the allowlist by construction.

The snapshot is also the one place activation state lives: activated skill
bodies stay active for the rest of the turn, duplicate activation is inert, and
every activation or activation failure is recorded for the Context Gearbox
ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aura.skills.text import SkillCandidate, SkillPack

#: Status values recorded in the activation ledger.
STATUS_ACTIVATED = "activated"
STATUS_ACTIVATION_FAILURE = "activation_failure"


@dataclass(frozen=True)
class SkillActivationRecord:
    """One activation (or activation failure) of a frozen candidate skill."""

    skill_id: str
    label: str
    provenance: str
    reason: str
    index_chars: int
    activated_chars: int
    body_hash: str
    status: str
    was_new: bool


class SkillTurnState:
    """Frozen candidate index plus activated-bodies ledger for one real turn."""

    def __init__(self, pack: SkillPack) -> None:
        self._candidates: tuple[SkillCandidate, ...] = tuple(pack.candidates)
        self._by_id: dict[str, SkillCandidate] = {
            candidate.skill_id: candidate for candidate in self._candidates
        }
        self._activated: dict[str, SkillCandidate] = {}
        self._log: list[SkillActivationRecord] = []

    # ---- frozen snapshot ---------------------------------------------------

    @property
    def candidates(self) -> tuple[SkillCandidate, ...]:
        """Frozen candidates in deterministic precedence order."""
        return self._candidates

    @property
    def skill_ids(self) -> tuple[str, ...]:
        return tuple(candidate.skill_id for candidate in self._candidates)

    @property
    def is_empty(self) -> bool:
        return not self._candidates

    def has_candidate(self, skill_id: str) -> bool:
        return skill_id in self._by_id

    def candidate(self, skill_id: str) -> SkillCandidate | None:
        return self._by_id.get(skill_id)

    # ---- activation ledger -------------------------------------------------

    @property
    def activated_ids(self) -> tuple[str, ...]:
        """Skill ids activated this turn, in activation order."""
        return tuple(self._activated.keys())

    def is_active(self, skill_id: str) -> bool:
        return skill_id in self._activated

    def activation_log(self) -> list[dict]:
        """Structured activation ledger: activated and failed skill activations.

        Inspection-only; never dumped into the provider prompt.
        """
        return [
            {
                "skill_id": record.skill_id,
                "label": record.label,
                "provenance": record.provenance,
                "reason": record.reason,
                "index_chars": record.index_chars,
                "activated_chars": record.activated_chars,
                "body_hash": record.body_hash,
                "status": record.status,
                "was_new": record.was_new,
            }
            for record in self._log
        ]

    def _record(
        self,
        candidate: SkillCandidate,
        *,
        status: str,
        was_new: bool,
        activated_chars: int,
    ) -> None:
        self._log.append(
            SkillActivationRecord(
                skill_id=candidate.skill_id,
                label=candidate.label,
                provenance=candidate.provenance,
                reason=candidate.reason,
                index_chars=candidate.index_chars,
                activated_chars=activated_chars,
                body_hash=candidate.body_hash,
                status=status,
                was_new=was_new,
            )
        )

    def _record_failure(self, skill_id: str, reason: str) -> None:
        self._log.append(
            SkillActivationRecord(
                skill_id=skill_id,
                label="",
                provenance="",
                reason=reason,
                index_chars=0,
                activated_chars=0,
                body_hash="",
                status=STATUS_ACTIVATION_FAILURE,
                was_new=False,
            )
        )

    # ---- resolution --------------------------------------------------------

    def resolve(self, skill_ids: list[str]) -> tuple[list[dict], list[dict]]:
        """Resolve requested ids against the frozen snapshot.

        Returns ``(activated, rejected)`` where every item is a plain dict for
        the structured tool result.  Resolution never touches the workspace,
        never re-scans the library, and never mutates the snapshot's candidate
        set — it only records which candidates the turn activated (and every
        failure, for the activation ledger).
        """
        activated: list[dict] = []
        rejected: list[dict] = []
        for raw_id in skill_ids:
            skill_id = _normalize_skill_id(raw_id)
            if not skill_id:
                rejected.append(
                    {
                        "skill_id": str(raw_id or ""),
                        "status": "malformed",
                        "reason": "skill id must be a non-empty string",
                    }
                )
                self._record_failure(str(raw_id or ""), "malformed skill id")
                continue
            candidate = self._by_id.get(skill_id)
            if candidate is None:
                rejected.append(
                    {
                        "skill_id": skill_id,
                        "status": "not_exposed_for_turn",
                        "reason": "not in the current turn's skill index",
                    }
                )
                self._record_failure(skill_id, "not in the current turn's skill index")
                continue
            if not candidate.skill.text:
                rejected.append(
                    {
                        "skill_id": skill_id,
                        "status": "unavailable",
                        "reason": "candidate has no loadable body",
                    }
                )
                self._record_failure(skill_id, "candidate has no loadable body")
                continue

            was_new = skill_id not in self._activated
            self._activated[skill_id] = candidate
            self._record(
                candidate,
                status=STATUS_ACTIVATED,
                was_new=was_new,
                activated_chars=candidate.body_chars,
            )
            activated.append(
                {
                    "skill_id": candidate.skill_id,
                    "label": candidate.label,
                    "provenance": candidate.provenance,
                    "reason": candidate.reason,
                    "description": candidate.description,
                    "body_hash": candidate.body_hash,
                    "body": candidate.skill.text,
                    "body_chars": candidate.body_chars,
                    "index_chars": candidate.index_chars,
                    "has_resources": candidate.has_resources,
                    "activated": "new" if was_new else "already_active",
                }
            )
        return activated, rejected


def _normalize_skill_id(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def load_skills_result(state: SkillTurnState, skill_ids: list[str]) -> dict:
    """Structured result payload for one ``load_skills`` tool call.

    Deterministic candidate order is preserved for the activated bodies;
    rejected ids are reported truthfully with a reason.  Duplicate activation
    is inert and reported as ``already_active``.
    """
    activated, rejected = state.resolve(skill_ids)
    return {
        "ok": True,
        "tool": "load_skills",
        "activated_count": len(activated),
        "rejected_count": len(rejected),
        "skills": activated,
        "rejected": rejected,
    }

