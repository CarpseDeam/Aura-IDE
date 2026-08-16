"""The prompt tells one coherent production-model story."""
from __future__ import annotations

from aura.context_gearbox.runtime import compose_system_prompt


def test_production_prompt_describes_a_continuous_checklist(tmp_path) -> None:
    prompt = compose_system_prompt("", tmp_path).system_prompt.lower()

    assert "update_task_checklist" in prompt
    assert "one continuous task" in prompt
    assert "separate assignments" in prompt


def test_production_prompt_expresses_material_evidence_closure(tmp_path) -> None:
    prompt = compose_system_prompt("", tmp_path).system_prompt.lower()

    # The old broad "do not make claims about repository contents you have
    # not verified" posture is gone; evidence is materially scoped instead.
    assert "do not make claims about repository contents" not in prompt
    assert "materially change the implementation" in prompt
    assert "adjacent uncertainty does not require investigation" in prompt
    # record_implementation_decision was removed in Phase 2C: the model
    # carries its implementation decision in the continuous conversation
    # instead of serializing it into a bookkeeping tool call.
    assert "record_implementation_decision" not in prompt


def test_review_implementation_plan_guidance_lives_with_its_tool_surface(
    tmp_path,
) -> None:
    """The global prompt no longer duplicates what the conditional schema owns."""
    prompt = compose_system_prompt("", tmp_path).system_prompt

    assert "review_implementation_plan" not in prompt
