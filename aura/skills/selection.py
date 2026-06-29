from __future__ import annotations

import re
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
    "worker",
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


def _content_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    return {
        token
        for token in re.split(r"[^a-z0-9_]+", value.lower())
        if len(token) >= 3 and token not in _CONTENT_STOPWORDS
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


def select_relevant_skills(
    skills: list[Skill],
    *,
    model: str | None = None,
    task_kind: str | None = None,
    target_files: tuple[str, ...] = (),
    content: str | None = None,
    limit: int = 5,
) -> list[Skill]:
    """Select skills relevant to the given terrain context.

    When any terrain argument is provided, only skills with at least one
    relevance signal are returned, scored and ranked.  When no terrain
    arguments are provided, returns the first *limit* skills.
    """
    content_tokens = _content_tokens(content)
    has_terrain = (
        model is not None
        or task_kind is not None
        or bool(target_files)
        or bool(content_tokens)
    )

    if not has_terrain:
        return skills[:limit]

    scored: list[tuple[int, Skill]] = []
    for skill in skills:
        score = 0
        # Model match
        if model is not None and skill.model == model:
            score += 2
        # Task kind match
        if task_kind is not None and skill.task_kinds and task_kind in skill.task_kinds:
            score += 2
        # File overlap
        if target_files and skill.path_globs:
            overlap = 0
            if skill.provenance == SkillProvenance.FAILURE_GRADUATED:
                # Graduated: use existing _paths_related matching (unchanged)
                for tf in target_files:
                    for pg in skill.path_globs:
                        if _paths_related(tf, pg):
                            overlap += 1
                            if overlap >= 2:
                                break
                    if overlap >= 2:
                        break
            else:
                # Bundled (and future provenances): directory-prefix matching
                for tf in target_files:
                    for pg in skill.path_globs:
                        if _bundled_path_matches(tf, pg):
                            overlap += 1
                            if overlap >= 2:
                                break
                    if overlap >= 2:
                        break
            score += min(overlap, 2)

        # Content relevance
        score += _content_overlap_score(content_tokens, _content_tokens(skill.text))

        if score == 0:
            continue
        scored.append((-score, skill))

    scored.sort(key=lambda x: x[0])
    return [skill for _, skill in scored[:limit]]
