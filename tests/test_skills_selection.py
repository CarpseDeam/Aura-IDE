from aura.skills.models import Skill, SkillProvenance
from aura.skills.selection import select_relevant_skills


def _skill(
    text: str,
    *,
    task_kinds: tuple[str, ...] = (),
    path_globs: tuple[str, ...] = (),
    provenance: SkillProvenance = SkillProvenance.USER_AUTHORED,
) -> Skill:
    return Skill(
        text=text,
        task_kinds=task_kinds,
        path_globs=path_globs,
        model=None,
        provenance=provenance,
        origin=(),
    )


def test_content_query_selects_cross_cutting_skill_without_terrain_metadata():
    relevant = _skill(
        "When editing authentication token refresh, validate session renewal behavior."
    )
    unrelated = _skill("When editing release packaging, validate installer artifacts.")

    selected = select_relevant_skills(
        [unrelated, relevant],
        content="Fix the auth token refresh regression in the login flow.",
    )

    assert selected == [relevant]


def test_no_terrain_or_content_keeps_existing_first_limit_behavior():
    skills = [_skill("First."), _skill("Second.")]

    assert select_relevant_skills(skills, limit=1) == [skills[0]]
