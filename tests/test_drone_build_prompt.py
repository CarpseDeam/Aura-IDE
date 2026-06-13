from __future__ import annotations

from aura.drones.build_prompt import build_drone_architect_prompt
from aura.drones.build_spec import DroneBuildBrief


def _prompt(build_brief: str = "Build a repo scout.") -> str:
    return build_drone_architect_prompt(
        DroneBuildBrief(
            response_type="brief",
            message="",
            ready_to_build=bool(build_brief),
            build_brief=build_brief,
        )
    )


def test_prompt_defines_folder_backed_drone() -> None:
    prompt = _prompt()
    assert "folder-backed artifact" in prompt
    assert "`drone.json`" in prompt
    assert "`main.py`" in prompt
    assert "`smoke.py`" in prompt


def test_prompt_requires_python_runtime_entrypoint_and_smoke() -> None:
    prompt = _prompt()
    assert "`runtime`: `python`" in prompt
    assert "`entrypoint`: normally `main:run`" in prompt
    assert "`smoke`: normally `smoke:run`" in prompt


def test_prompt_tells_planner_to_dispatch_worker_and_register_folder() -> None:
    prompt = _prompt()
    assert "dispatch one Worker" in prompt
    assert "complete Drone folder" in prompt
    assert "register_drone_folder" in prompt


def test_prompt_does_not_mention_removed_builder_tool() -> None:
    prompt = _prompt()
    removed_tool = "save" + "_drone_definition"
    assert removed_tool not in prompt
    assert "Compiled Build Plan" not in prompt
    assert "DroneDefinition" not in prompt


def test_prompt_mentions_allowed_tools_only_as_empty_compatibility_field() -> None:
    prompt = _prompt()
    assert "- `allowed_tools`: `[]` for new folder-backed Drones" in prompt
    assert "menu of existing harness tools" in prompt


def test_prompt_includes_build_brief_verbatim() -> None:
    brief = "Watch Reddit and Hacker News for Rust compiler leads."
    assert brief in _prompt(brief)


def test_prompt_asks_for_description_when_no_brief() -> None:
    prompt = _prompt("")
    assert 'Describe the Drone you want to build.' in prompt
