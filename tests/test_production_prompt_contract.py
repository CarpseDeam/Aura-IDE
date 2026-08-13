"""The prompt tells one coherent production-model story."""
from __future__ import annotations

from aura.context_gearbox.runtime import compose_system_prompt


def test_production_prompt_describes_a_continuous_checklist(tmp_path) -> None:
    prompt = compose_system_prompt("", tmp_path).system_prompt.lower()

    assert "update_task_checklist" in prompt
    assert "one continuous task" in prompt
    assert "separate assignments" in prompt
