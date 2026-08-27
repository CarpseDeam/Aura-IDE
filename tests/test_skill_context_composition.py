"""Production prompt composition carries no runtime role."""

from __future__ import annotations

from pathlib import Path

from aura.context_gearbox.runtime import compose_system_prompt


def test_production_prompt_composes_without_runtime_roles(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "player.py").write_text("value = 1\n", encoding="utf-8")

    composed = compose_system_prompt(
        tmp_path,
        model="deepseek-chat",
        task_kind="bugfix",
        target_files=("scripts/player.py",),
        content="fix the signal bug",
    )

    assert composed.system_prompt
    assert not hasattr(composed, "role")
