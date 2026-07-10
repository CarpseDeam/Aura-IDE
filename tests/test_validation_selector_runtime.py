from __future__ import annotations

from aura.bridge.validation_selector_runtime import combine_validation_commands
from aura.work_artifact.model import ValidationCommandSpec


def test_combine_preserves_structured_planner_validation_commands() -> None:
    planned = ValidationCommandSpec(
        command="python -m pytest tests/test_player.py -q",
        cwd="game",
        expected_outcome="tests pass",
    )

    combined = combine_validation_commands(
        [planned],
        ["godot --headless --path game --import"],
    )

    assert combined == [planned, "godot --headless --path game --import"]
    assert combined[0] is planned


def test_combine_deduplicates_by_command_and_working_directory() -> None:
    first = ValidationCommandSpec(command="python -m pytest -q", cwd="game")
    duplicate = ValidationCommandSpec(command="  python   -m pytest -q  ", cwd="game/")
    other_package = ValidationCommandSpec(command="python -m pytest -q", cwd="server")

    assert combine_validation_commands([first, duplicate, other_package], []) == [
        first,
        other_package,
    ]
