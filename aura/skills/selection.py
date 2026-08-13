from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from aura.skills.models import Skill, SkillProvenance

_CONTENT_STOPWORDS = {
    "about",
    "acceptance",
    "after",
    "again",
    "against",
    "allowed",
    "also",
    "and",
    "any",
    "are",
    "before",
    "builder",
    "but",
    "can",
    "cannot",
    "change",
    "changes",
    "code",
    "context",
    "done",
    "for",
    "forbidden",
    "from",
    "goal",
    "has",
    "have",
    "implementation",
    "into",
    "kind",
    "listed",
    "make",
    "must",
    "need",
    "needs",
    "none",
    "not",
    "note",
    "only",
    "output",
    "outputs",
    "required",
    "responsibilities",
    "risk",
    "shape",
    "should",
    "spec",
    "task",
    "that",
    "the",
    "this",
    "to",
    "update",
    "use",
    "validate",
    "validation",
    "verified",
    "verify",
    "when",
    "with",
}


def _paths_related(a: str, b: str) -> bool:
    """Return True if two workspace paths share a common non-root directory
    prefix (>=1 component) or one is a parent directory of the other."""
    a_parts = Path(a).parent.parts
    b_parts = Path(b).parent.parts
    if not a_parts or not b_parts:
        return False
    common = 0
    for pa, pb in zip(a_parts, b_parts):
        if pa == pb:
            common += 1
        else:
            break
    return common >= 1 or Path(a).parent == Path(b) or Path(b).parent == Path(a)


def _bundled_path_matches(tf: str, pg: str) -> bool:
    """Check if target file *tf* is within the path prefix/glob *pg*.

    - If *pg* contains wildcard characters (*, ?, [, ]), use fnmatch.
    - If *pg* ends with a trailing slash, treat it as a directory prefix.
    - Otherwise treat it as a directory prefix (no wildcard = plain path prefix).
    """
    norm_tf = tf.replace("\\", "/")
    norm_pg = pg.replace("\\", "/")

    # If the glob contains wildcards, delegate to fnmatch
    if any(ch in norm_pg for ch in "*?[]"):
        return fnmatchcase(norm_tf, norm_pg)

    # Strip trailing slash for prefix check
    prefix = norm_pg.rstrip("/")
    if not prefix:
        return False

    return norm_tf == prefix or norm_tf.startswith(prefix + "/")


def _content_tokens(value: str | None, *, apply_stopwords: bool = True) -> set[str]:
    if not value:
        return set()
    return {
        token
        for token in re.split(r"[^a-z0-9_]+", value.lower())
        if len(token) >= 3 and (not apply_stopwords or token not in _CONTENT_STOPWORDS)
    }


def _tokens_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) < 4 or len(right) < 4:
        return False
    return left.startswith(right) or right.startswith(left)


def _content_overlap_score(query_tokens: set[str], skill_tokens: set[str]) -> int:
    if not query_tokens or not skill_tokens:
        return 0
    matched_skill_tokens: set[str] = set()
    score = 0
    for query_token in sorted(query_tokens):
        for skill_token in sorted(skill_tokens):
            if skill_token in matched_skill_tokens:
                continue
            if _tokens_match(query_token, skill_token):
                matched_skill_tokens.add(skill_token)
                score += 1
                break
    return min(score, 3)


# Bounded, but not stingy: relevance gating (not this cap) is what keeps
# unrelated guidance out, so the cap only exists to stop a pathological pile-up
# on a broad request.
DEFAULT_SKILL_LIMIT = 8

# A skill earns its slot with a hard terrain signal (model, task kind, or
# target path), with curated triggers, or with a strong body-token overlap.
# One incidental fuzzy token is ranking noise, not relevance — admitting it is
# what dumps a whole domain pack into unrelated requests.
_MIN_TRIGGER_ONLY_SCORE = 2
_MIN_CONTENT_ONLY_SCORE = 3


@dataclass(frozen=True)
class ScoredSkill:
    """One skill with its terrain score and the signals that produced it."""

    skill: Skill
    score: int
    reasons: tuple[str, ...]
    hard_score: int = 0
    trigger_score: int = 0
    content_score: int = 0

    @property
    def declares_selectors(self) -> bool:
        """True when the skill states how it wants to be selected."""
        skill = self.skill
        return bool(skill.task_kinds or skill.path_globs or skill.triggers)

    @property
    def is_relevant(self) -> bool:
        """True when the terrain signal is strong enough to load the skill.

        Body-token overlap alone is length-biased — the longest skill overlaps
        something in every request — so it only admits skills that declare no
        selectors of their own. A skill that declares task kinds, path globs,
        or triggers is selected on those declarations.
        """
        if self.hard_score > 0:
            return True
        if self.trigger_score >= _MIN_TRIGGER_ONLY_SCORE:
            return True
        if self.declares_selectors:
            return False
        return self.content_score >= _MIN_CONTENT_ONLY_SCORE

    @property
    def rejection_reason(self) -> str:
        if self.score <= 0:
            return "no terrain relevance signal"
        if self.declares_selectors and self.hard_score == 0:
            return (
                "no declared task kind, path, or trigger matched "
                f"(body overlap only, score {self.score})"
            )
        return f"only weak terrain overlap (score {self.score})"


def has_terrain_signal(
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    content: str | None = None,
) -> bool:
    """Return True when the caller supplied any live terrain to select against."""
    return (
        model is not None
        or task_kind is not None
        or bool(target_files)
        or bool(_content_tokens(content))
    )


def score_skills(
    skills: list[Skill],
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    content: str | None = None,
) -> list[ScoredSkill]:
    """Score every skill against the terrain, best first.

    Ties keep the caller's read order (workspace-authored, then packaged, then
    learned guards), so scoping priority survives ranking.  Zero-score skills
    are returned too, with an empty reason tuple, so callers can report why a
    skill was not loaded.
    """
    content_tokens = _content_tokens(content)
    trigger_query_tokens = _content_tokens(content, apply_stopwords=False)

    scored: list[tuple[int, int, ScoredSkill]] = []
    for index, skill in enumerate(skills):
        score = 0
        hard_score = 0
        reasons: list[str] = []
        # Model match
        if model is not None and skill.model == model:
            score += 2
            hard_score += 2
            reasons.append(f"model match ({model})")
        # Task kind match
        if task_kind is not None and skill.task_kinds and task_kind in skill.task_kinds:
            score += 2
            hard_score += 2
            reasons.append(f"task kind match ({task_kind})")
        # File overlap
        if target_files and skill.path_globs:
            overlap = 0
            matched_globs: list[str] = []
            if skill.provenance == SkillProvenance.FAILURE_GRADUATED:
                # Graduated: use existing _paths_related matching (unchanged)
                matcher = _paths_related
            else:
                # Bundled (and future provenances): directory-prefix matching
                matcher = _bundled_path_matches
            for tf in target_files:
                for pg in skill.path_globs:
                    if matcher(tf, pg):
                        overlap += 1
                        matched_globs.append(pg)
                        if overlap >= 2:
                            break
                if overlap >= 2:
                    break
            if overlap:
                score += min(overlap, 2)
                hard_score += min(overlap, 2)
                reasons.append("path match (" + ", ".join(matched_globs) + ")")

        # Content relevance
        content_score = _content_overlap_score(content_tokens, _content_tokens(skill.text))
        if content_score:
            score += content_score
            reasons.append(f"content overlap ({content_score})")
        trigger_score = _content_overlap_score(
            trigger_query_tokens,
            _content_tokens(" ".join(skill.triggers), apply_stopwords=False),
        )
        if trigger_score:
            score += trigger_score
            reasons.append(f"trigger match ({trigger_score})")

        scored.append(
            (
                -score,
                index,
                ScoredSkill(
                    skill=skill,
                    score=score,
                    reasons=tuple(reasons),
                    hard_score=hard_score,
                    trigger_score=trigger_score,
                    content_score=content_score,
                ),
            )
        )

    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored]


def select_relevant_skills(
    skills: list[Skill],
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    content: str | None = None,
    limit: int = DEFAULT_SKILL_LIMIT,
) -> list[Skill]:
    """Select skills relevant to the given terrain context.

    When any terrain argument is provided, only skills with a strong enough
    relevance signal are returned, scored and ranked.  When no terrain
    arguments are provided, returns the first *limit* skills.
    """
    if not has_terrain_signal(
        model=model,
        task_kind=task_kind,
        target_files=target_files,
        content=content,
    ):
        return skills[:limit]

    ranked = score_skills(
        skills,
        model=model,
        task_kind=task_kind,
        target_files=target_files,
        content=content,
    )
    return [item.skill for item in ranked if item.is_relevant][:limit]
