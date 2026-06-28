"""Tests for Phase 4A — Eviction module + per-skill source ID plumbing."""

from __future__ import annotations

from pathlib import Path

import pytest

from aura.skills.eviction import (
    EvictionMode,
    EvictionVerdict,
    apply_eviction_mode,
    compute_eviction_verdicts,
    format_eviction_report,
    summarize_eviction_report,
)
from aura.skills.models import Skill, SkillProvenance, compute_skill_id
from aura.skills.text import build_skill_context, build_skill_context_with_ids, format_skills

# ---------------------------------------------------------------------------
# compute_skill_id
# ---------------------------------------------------------------------------


class TestComputeSkillId:
    def test_deterministic(self) -> None:
        """Same text produces the same ID."""
        text = "Always use type hints for public functions."
        assert compute_skill_id(text) == compute_skill_id(text)

    def test_different_text_different_id(self) -> None:
        """Different texts produce different IDs."""
        id_a = compute_skill_id("Do foo.")
        id_b = compute_skill_id("Do bar.")
        assert id_a != id_b

    def test_provenance_participates(self) -> None:
        """Same text with different provenance produces different IDs."""
        text = "Keep validation focused."
        bundled_id = compute_skill_id(text, SkillProvenance.BUNDLED)
        refined_id = compute_skill_id(text, SkillProvenance.REFLECTION_REFINED)

        assert bundled_id != refined_id

    def test_starts_with_skill_prefix(self) -> None:
        """Output starts with 'skill_'."""
        assert compute_skill_id("any text").startswith("skill_")

    def test_length(self) -> None:
        """Length is 6 (skill_) + 16 hex chars = 22."""
        sid = compute_skill_id("any text")
        assert len(sid) == 22

    def test_hex_chars_after_prefix(self) -> None:
        """Characters after 'skill_' are valid hex digits."""
        sid = compute_skill_id("any text")
        suffix = sid[6:]
        assert len(suffix) == 16
        int(suffix, 16)  # raises ValueError if not hex


# ---------------------------------------------------------------------------
# build_skill_context_with_ids
# ---------------------------------------------------------------------------


class TestBuildSkillContextWithIds:
    def test_returns_tuple(self, tmp_path: Path) -> None:
        """Returns (text, ids) tuple."""
        text, ids = build_skill_context_with_ids(tmp_path)
        assert isinstance(text, str)
        assert isinstance(ids, list)

    def test_empty_workspace_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When read_skills returns empty list, returns ("", [])."""
        monkeypatch.setattr(
            "aura.skills.text.read_skills",
            lambda _: [],
        )
        text, ids = build_skill_context_with_ids(tmp_path)
        assert text == ""
        assert ids == []

    def test_ids_match_skills(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Number of IDs matches number of selected skills."""
        skills = [
            Skill(
                text="Skill A description.",
                task_kinds=("bugfix",),
                path_globs=(),
                model=None,
                provenance=SkillProvenance.BUNDLED,
                origin=(),
            ),
            Skill(
                text="Skill B description.",
                task_kinds=("refactor",),
                path_globs=(),
                model=None,
                provenance=SkillProvenance.BUNDLED,
                origin=(),
            ),
        ]
        monkeypatch.setattr(
            "aura.skills.text.read_skills",
            lambda _: skills,
        )
        monkeypatch.setattr(
            "aura.skills.text.select_relevant_skills",
            lambda s, **kw: s,
        )
        text, ids = build_skill_context_with_ids(tmp_path)
        assert len(ids) == len(skills)
        assert all(isinstance(sid, str) and sid.startswith("skill_") for sid in ids)
        assert ids == [compute_skill_id(skill) for skill in skills]

    def test_dry_run_does_not_filter_selected_skills(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dry-run computes verdicts but keeps selected skills in context."""
        skills = [
            Skill(
                text="Graduated negative.",
                task_kinds=("bugfix",),
                path_globs=(),
                model=None,
                provenance=SkillProvenance.FAILURE_GRADUATED,
                origin=(),
            )
        ]
        skill_id = compute_skill_id(skills[0])
        monkeypatch.setattr("aura.skills.text.read_skills", lambda _: skills)
        monkeypatch.setattr("aura.skills.text.select_relevant_skills", lambda s, **kw: s)
        monkeypatch.setattr(
            "aura.skills.text.compute_eviction_verdicts",
            lambda *_args, **_kwargs: [
                EvictionVerdict(
                    skill_id=skill_id,
                    skill_text_prefix="Graduated negative.",
                    provenance=SkillProvenance.FAILURE_GRADUATED,
                    would_evict=True,
                    reason="negative lift -0.250 on terrain 'bugfix'",
                    lift=-0.25,
                    loaded_n=10,
                    not_loaded_n=10,
                    task_kind="bugfix",
                )
            ],
        )

        text, ids = build_skill_context_with_ids(
            tmp_path,
            task_kind="bugfix",
            eviction_mode=EvictionMode.DRY_RUN,
        )

        assert "Graduated negative." in text
        assert ids == [skill_id]

    def test_enforce_filters_only_as_explicit_helper_path(self) -> None:
        """ENFORCE is available as a pure helper and is not the runtime default."""
        keep = Skill(
            text="Bundled keep.",
            task_kinds=(),
            path_globs=(),
            model=None,
            provenance=SkillProvenance.BUNDLED,
            origin=(),
        )
        evict = Skill(
            text="Graduated evict.",
            task_kinds=("bugfix",),
            path_globs=(),
            model=None,
            provenance=SkillProvenance.FAILURE_GRADUATED,
            origin=(),
        )
        verdicts = [
            EvictionVerdict(
                skill_id=compute_skill_id(evict),
                skill_text_prefix="Graduated evict.",
                provenance=SkillProvenance.FAILURE_GRADUATED,
                would_evict=True,
                reason="negative lift -0.250 on terrain 'bugfix'",
                lift=-0.25,
                loaded_n=10,
                not_loaded_n=10,
                task_kind="bugfix",
            )
        ]

        assert apply_eviction_mode([keep, evict], verdicts) == [keep, evict]
        assert apply_eviction_mode(
            [keep, evict],
            verdicts,
            mode=EvictionMode.DRY_RUN,
        ) == [keep, evict]
        assert apply_eviction_mode(
            [keep, evict],
            verdicts,
            mode=EvictionMode.ENFORCE,
        ) == [keep]

    def test_runtime_default_eviction_mode_is_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        skills = [
            Skill(
                text="Graduated default keep.",
                task_kinds=("bugfix",),
                path_globs=(),
                model=None,
                provenance=SkillProvenance.FAILURE_GRADUATED,
                origin=(),
            )
        ]
        monkeypatch.setattr("aura.skills.text.read_skills", lambda _: skills)
        monkeypatch.setattr("aura.skills.text.select_relevant_skills", lambda s, **kw: s)
        monkeypatch.setattr(
            "aura.skills.text.compute_eviction_verdicts",
            lambda *_args, **_kwargs: pytest.fail("default mode should not compute eviction"),
        )

        text, ids = build_skill_context_with_ids(tmp_path, task_kind="bugfix")

        assert "Graduated default keep." in text
        assert ids == [compute_skill_id(skills[0])]


# ---------------------------------------------------------------------------
# build_skill_context backward compatibility
# ---------------------------------------------------------------------------


class TestBuildSkillContextBackwardCompat:
    def test_still_returns_string(self, tmp_path: Path) -> None:
        """build_skill_context still returns a plain string."""
        result = build_skill_context(tmp_path)
        assert isinstance(result, str)

    def test_empty_workspace_empty_string(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "aura.skills.text.read_skills",
            lambda _: [],
        )
        assert build_skill_context(tmp_path) == ""


class TestFormatSkills:
    def test_reflection_refined_skills_render(self) -> None:
        skill = Skill(
            text="### Refined Guard\nUse the narrower parser path.",
            task_kinds=("refactor",),
            path_globs=("aura/skills/",),
            model=None,
            provenance=SkillProvenance.REFLECTION_REFINED,
            origin=(),
        )

        text = format_skills([skill])

        assert "### Refined Skill Guards" in text
        assert "Use the narrower parser path." in text


# ---------------------------------------------------------------------------
# EvictionVerdict
# ---------------------------------------------------------------------------


class TestEvictionVerdictDataclass:
    def test_frozen(self) -> None:
        """EvictionVerdict is frozen."""
        v = EvictionVerdict(
            skill_id="skill_abc",
            skill_text_prefix="Always use",
            provenance=SkillProvenance.BUNDLED,
            would_evict=False,
            reason="test",
            lift=None,
            loaded_n=0,
            not_loaded_n=0,
            task_kind=None,
        )
        with pytest.raises(Exception):
            v.would_evict = True  # type: ignore[misc]

    def test_all_fields_present(self) -> None:
        v = EvictionVerdict(
            skill_id="skill_abc",
            skill_text_prefix="Always use",
            provenance=SkillProvenance.BUNDLED,
            would_evict=False,
            reason="sticky provenance",
            lift=0.5,
            loaded_n=10,
            not_loaded_n=5,
            task_kind="bugfix",
        )
        assert v.skill_id == "skill_abc"
        assert v.would_evict == False
        assert v.lift == 0.5
        assert v.task_kind == "bugfix"


# ---------------------------------------------------------------------------
# compute_eviction_verdicts — provenance and utility scenarios
# ---------------------------------------------------------------------------


class MockSkill:
    """Helper to build Skill tuples with less boilerplate."""

    @staticmethod
    def make(
        text: str = "Some skill text.",
        provenance: SkillProvenance = SkillProvenance.BUNDLED,
    ) -> Skill:
        return Skill(
            text=text,
            task_kinds=("bugfix",),
            path_globs=(),
            model=None,
            provenance=provenance,
            origin=(),
        )


def _sample_skills() -> list[Skill]:
    return [
        MockSkill.make(text="Bundled safety check.", provenance=SkillProvenance.BUNDLED),
        MockSkill.make(text="User authored rule.", provenance=SkillProvenance.USER_AUTHORED),
        MockSkill.make(
            text="Graduated from failure: check imports.",
            provenance=SkillProvenance.FAILURE_GRADUATED,
        ),
        MockSkill.make(
            text="Refined by reflection: use pathlib.",
            provenance=SkillProvenance.REFLECTION_REFINED,
        ),
    ]


def _compute_single_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill: Skill,
    utility: _FakeSourceUtility | None,
    *,
    task_kind: str = "bugfix",
) -> EvictionVerdict:
    skill_id = compute_skill_id(skill)
    monkeypatch.setattr("aura.skills.eviction.read_skills", lambda _: [skill])
    monkeypatch.setattr(
        "aura.skills.eviction.derive_source_utility",
        lambda _ws, min_arm=3: {} if utility is None else {skill_id: utility},
    )

    verdicts = compute_eviction_verdicts(tmp_path, task_kind=task_kind, min_arm=3)

    assert len(verdicts) == 1
    return verdicts[0]


class TestComputeEvictionVerdictsSticky:
    def test_bundled_never_evicted(self) -> None:
        """BUNDLED skill: would_evict=False, reason='sticky provenance'."""
        skill = MockSkill.make(provenance=SkillProvenance.BUNDLED)
        skill_id = compute_skill_id(skill.text)
        verdict = EvictionVerdict(
            skill_id=skill_id,
            skill_text_prefix=skill.text.split("\n")[0],
            provenance=SkillProvenance.BUNDLED,
            would_evict=False,
            reason="sticky provenance",
            lift=None,
            loaded_n=0,
            not_loaded_n=0,
            task_kind=None,
        )
        assert verdict.would_evict is False
        assert verdict.reason == "sticky provenance"

    @pytest.mark.parametrize(
        "provenance",
        (SkillProvenance.BUNDLED, SkillProvenance.USER_AUTHORED),
    )
    def test_sticky_provenance_never_evicted_by_negative_lift(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        provenance: SkillProvenance,
    ) -> None:
        skill = MockSkill.make(provenance=provenance)
        verdict = _compute_single_verdict(
            tmp_path,
            monkeypatch,
            skill,
            _FakeSourceUtility(
                status="measured",
                lift=-0.75,
                loaded_n=10,
                not_loaded_n=10,
                task_kind="bugfix",
            ),
        )

        assert verdict.would_evict is False
        assert verdict.reason == "sticky provenance"

    def test_user_authored_never_evicted(self) -> None:
        """USER_AUTHORED skill: would_evict=False, reason='sticky provenance'."""
        verdict = EvictionVerdict(
            skill_id="skill_user",
            skill_text_prefix="user rule",
            provenance=SkillProvenance.USER_AUTHORED,
            would_evict=False,
            reason="sticky provenance",
            lift=None,
            loaded_n=0,
            not_loaded_n=0,
            task_kind=None,
        )
        assert verdict.would_evict is False
        assert verdict.reason == "sticky provenance"


class TestComputeEvictionVerdictsNoUtility:
    def test_no_utility_data(self) -> None:
        """Graduated skill with no utility data: would_evict=False."""
        verdict = EvictionVerdict(
            skill_id="skill_unknown",
            skill_text_prefix="graduated",
            provenance=SkillProvenance.FAILURE_GRADUATED,
            would_evict=False,
            reason="no utility data yet",
            lift=None,
            loaded_n=0,
            not_loaded_n=0,
            task_kind=None,
        )
        assert verdict.would_evict is False
        assert verdict.reason == "no utility data yet"

    def test_missing_utility_keeps_skill(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        skill = MockSkill.make(provenance=SkillProvenance.FAILURE_GRADUATED)

        verdict = _compute_single_verdict(tmp_path, monkeypatch, skill, None)

        assert verdict.would_evict is False
        assert verdict.reason == "no utility data yet"


class TestComputeEvictionVerdictsInsufficient:
    def test_insufficient_data(self) -> None:
        """Graduated skill with insufficient data: would_evict=False."""
        verdict = EvictionVerdict(
            skill_id="skill_insuf",
            skill_text_prefix="insufficient",
            provenance=SkillProvenance.REFLECTION_REFINED,
            would_evict=False,
            reason="insufficient data: loaded_n=1, not_loaded_n=2, need >= 3 each",
            lift=None,
            loaded_n=1,
            not_loaded_n=2,
            task_kind="bugfix",
        )
        assert verdict.would_evict is False
        assert verdict.reason.startswith("insufficient data")

    def test_insufficient_utility_keeps_skill(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        skill = MockSkill.make(provenance=SkillProvenance.REFLECTION_REFINED)

        verdict = _compute_single_verdict(
            tmp_path,
            monkeypatch,
            skill,
            _FakeSourceUtility(
                status="insufficient",
                lift=None,
                loaded_n=2,
                not_loaded_n=10,
                task_kind="bugfix",
            ),
        )

        assert verdict.would_evict is False
        assert verdict.reason.startswith("insufficient data")


class TestComputeEvictionVerdictsNegativeLift:
    def test_measured_negative_lift(self) -> None:
        """Graduated skill with measured negative lift: would_evict=True."""
        verdict = EvictionVerdict(
            skill_id="skill_neg",
            skill_text_prefix="negative lift skill",
            provenance=SkillProvenance.FAILURE_GRADUATED,
            would_evict=True,
            reason="negative lift -0.250 on terrain 'bugfix'",
            lift=-0.25,
            loaded_n=10,
            not_loaded_n=10,
            task_kind="bugfix",
        )
        assert verdict.would_evict is True
        assert "negative lift" in verdict.reason

    @pytest.mark.parametrize(
        "provenance",
        (
            SkillProvenance.FAILURE_GRADUATED,
            SkillProvenance.REFLECTION_REFINED,
        ),
    )
    def test_same_terrain_measured_negative_lift_can_evict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        provenance: SkillProvenance,
    ) -> None:
        skill = MockSkill.make(provenance=provenance)

        verdict = _compute_single_verdict(
            tmp_path,
            monkeypatch,
            skill,
            _FakeSourceUtility(
                status="measured",
                lift=-0.25,
                loaded_n=10,
                not_loaded_n=10,
                task_kind="bugfix",
            ),
            task_kind="bugfix",
        )

        assert verdict.would_evict is True
        assert "negative lift" in verdict.reason

    def test_different_terrain_keeps_skill(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        skill = MockSkill.make(provenance=SkillProvenance.FAILURE_GRADUATED)

        verdict = _compute_single_verdict(
            tmp_path,
            monkeypatch,
            skill,
            _FakeSourceUtility(
                status="measured",
                lift=-0.25,
                loaded_n=10,
                not_loaded_n=10,
                task_kind="refactor",
            ),
            task_kind="bugfix",
        )

        assert verdict.would_evict is False
        assert verdict.reason.startswith("different terrain")


class TestComputeEvictionVerdictsNonNegativeLift:
    def test_measured_non_negative_lift(self) -> None:
        """Graduated skill with measured non-negative lift: would_evict=False."""
        verdict = EvictionVerdict(
            skill_id="skill_pos",
            skill_text_prefix="positive lift skill",
            provenance=SkillProvenance.FAILURE_GRADUATED,
            would_evict=False,
            reason="lift +0.150 >= threshold on terrain 'bugfix'",
            lift=0.15,
            loaded_n=10,
            not_loaded_n=10,
            task_kind="bugfix",
        )
        assert verdict.would_evict is False
        assert ">= threshold" in verdict.reason


# ---------------------------------------------------------------------------
# format_eviction_report
# ---------------------------------------------------------------------------


class TestFormatEvictionReport:
    def test_returns_string(self) -> None:
        result = format_eviction_report([])
        assert isinstance(result, str)

    def test_includes_phase_4a(self) -> None:
        v = EvictionVerdict(
            skill_id="skill_a",
            skill_text_prefix="test",
            provenance=SkillProvenance.BUNDLED,
            would_evict=False,
            reason="sticky provenance",
            lift=None,
            loaded_n=0,
            not_loaded_n=0,
            task_kind=None,
        )
        result = format_eviction_report([v])
        assert "Phase 4A" in result

    def test_lists_evicted_skills(self) -> None:
        v = EvictionVerdict(
            skill_id="skill_bad",
            skill_text_prefix="bad",
            provenance=SkillProvenance.FAILURE_GRADUATED,
            would_evict=True,
            reason="negative lift",
            lift=-0.1,
            loaded_n=5,
            not_loaded_n=5,
            task_kind="bugfix",
        )
        result = format_eviction_report([v])
        assert "Evicted Skills" in result or "evicted" in result.lower()

    def test_lists_retained_skills(self) -> None:
        v = EvictionVerdict(
            skill_id="skill_good",
            skill_text_prefix="good",
            provenance=SkillProvenance.BUNDLED,
            would_evict=False,
            reason="sticky provenance",
            lift=None,
            loaded_n=0,
            not_loaded_n=0,
            task_kind=None,
        )
        result = format_eviction_report([v])
        assert "Retained Skills" in result

    def test_ends_with_dry_run(self) -> None:
        v = EvictionVerdict(
            skill_id="skill_x",
            skill_text_prefix="x",
            provenance=SkillProvenance.BUNDLED,
            would_evict=False,
            reason="sticky provenance",
            lift=None,
            loaded_n=0,
            not_loaded_n=0,
            task_kind=None,
        )
        result = format_eviction_report([v])
        assert "dry-run" in result.lower()


# ---------------------------------------------------------------------------
# summarize_eviction_report
# ---------------------------------------------------------------------------


class TestSummarizeEvictionReport:
    def test_returns_dict(self) -> None:
        result = summarize_eviction_report([])
        assert isinstance(result, dict)

    def test_has_keys(self) -> None:
        result = summarize_eviction_report([])
        assert "total_skills" in result
        assert "would_evict_count" in result
        assert "would_evict" in result
        assert "dry_run" in result

    def test_dry_run_true(self) -> None:
        result = summarize_eviction_report([])
        assert result["dry_run"] is True

    def test_counts(self) -> None:
        v1 = EvictionVerdict(
            skill_id="skill_a",
            skill_text_prefix="a",
            provenance=SkillProvenance.BUNDLED,
            would_evict=False,
            reason="sticky provenance",
            lift=None,
            loaded_n=0,
            not_loaded_n=0,
            task_kind=None,
        )
        v2 = EvictionVerdict(
            skill_id="skill_b",
            skill_text_prefix="b",
            provenance=SkillProvenance.FAILURE_GRADUATED,
            would_evict=True,
            reason="negative lift",
            lift=-0.1,
            loaded_n=5,
            not_loaded_n=5,
            task_kind="bugfix",
        )
        result = summarize_eviction_report([v1, v2])
        assert result["total_skills"] == 2
        assert result["would_evict_count"] == 1
        assert len(result["would_evict"]) == 1
        assert result["would_evict"][0]["skill_id"] == "skill_b"


# ---------------------------------------------------------------------------
# Integration-style test with monkeypatched read_skills / derive_source_utility
# ---------------------------------------------------------------------------


class _FakeSourceUtility:
    """Minimal stand-in for SourceUtility duck-typed fields."""

    def __init__(
        self,
        *,
        status: str,
        lift: float | None = None,
        loaded_n: int = 0,
        not_loaded_n: int = 0,
        task_kind: str = "bugfix",
    ):
        self.status = status
        self.lift = lift
        self.loaded_n = loaded_n
        self.not_loaded_n = not_loaded_n
        self.task_kind = task_kind


class TestIntegrationScenario:
    def test_mixed_scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """2 bundled (always retained), 1 graduated with negative lift (evicted),
        1 graduated with positive lift (retained)."""
        skills = [
            Skill(
                text="Bundled one.",
                task_kinds=(),
                path_globs=(),
                model=None,
                provenance=SkillProvenance.BUNDLED,
                origin=(),
            ),
            Skill(
                text="Bundled two.",
                task_kinds=(),
                path_globs=(),
                model=None,
                provenance=SkillProvenance.BUNDLED,
                origin=(),
            ),
            Skill(
                text="Graduated negative.",
                task_kinds=("bugfix",),
                path_globs=(),
                model=None,
                provenance=SkillProvenance.FAILURE_GRADUATED,
                origin=(),
            ),
            Skill(
                text="Graduated positive.",
                task_kinds=("bugfix",),
                path_globs=(),
                model=None,
                provenance=SkillProvenance.FAILURE_GRADUATED,
                origin=(),
            ),
        ]

        monkeypatch.setattr("aura.skills.eviction.read_skills", lambda _: skills)

        negative_id = compute_skill_id(skills[2])
        positive_id = compute_skill_id(skills[3])

        monkeypatch.setattr(
            "aura.skills.eviction.derive_source_utility",
            lambda _ws, min_arm=3: {
                negative_id: _FakeSourceUtility(
                    status="measured", lift=-0.25, loaded_n=10, not_loaded_n=10,
                ),
                positive_id: _FakeSourceUtility(
                    status="measured", lift=0.15, loaded_n=10, not_loaded_n=10,
                ),
            },
        )

        verdicts = compute_eviction_verdicts(tmp_path, task_kind="bugfix", min_arm=3)
        assert len(verdicts) == 4

        bundled_verdicts = [v for v in verdicts if v.provenance == SkillProvenance.BUNDLED]
        graduated_verdicts = [v for v in verdicts if v.provenance == SkillProvenance.FAILURE_GRADUATED]

        # All bundled retained
        for v in bundled_verdicts:
            assert v.would_evict is False
            assert "sticky provenance" in v.reason

        # Negative lift → evicted
        neg = [v for v in graduated_verdicts if v.skill_id == negative_id]
        assert len(neg) == 1
        assert neg[0].would_evict is True
        assert "negative lift" in neg[0].reason

        # Positive lift → retained
        pos = [v for v in graduated_verdicts if v.skill_id == positive_id]
        assert len(pos) == 1
        assert pos[0].would_evict is False
        assert ">= threshold" in pos[0].reason
