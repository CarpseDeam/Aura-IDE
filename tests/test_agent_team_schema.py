"""Focused contract tests for Aura's model-facing automatic-team schema."""
from __future__ import annotations

from typing import Any

import jsonschema

from aura.conversation.tools.schemas.agent_teams import (
    MAX_AGENT_TEAM_OCCURRENCES,
    build_run_agent_team_tool_def,
)


def _parameters(tool: dict[str, Any]) -> dict[str, Any]:
    return tool["function"]["parameters"]


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _valid_team() -> dict[str, Any]:
    return {
        "task": "Investigate the bug, implement the fix, and review the result.",
        "team_name": "Repair team",
        "team_description": "Parallel investigation followed by one review.",
        "new_agents": [
            {
                "alias": "investigator",
                "name": "Investigator",
                "description": "Finds the cause using repository evidence.",
                "instructions": "Inspect relevant code and report demonstrated causes.",
                "model_target": "local-fast",
                "thinking": "off",
                "permission": "read_only",
            },
            {
                "alias": "implementer",
                "name": "Implementer",
                "description": "Makes the focused code change.",
                "instructions": "Implement the requested change cleanly and test it.",
                "model_target": "inherit",
                "thinking": "high",
                "permission": "read_write",
            },
            {
                "alias": "migrator",
                "name": "Migrator",
                "description": "Updates the related compatibility path.",
                "instructions": "Own compatibility edits and preserve existing behavior.",
                "model_target": "hosted-strong",
                "thinking": "max",
                "permission": "read_write",
            },
            {
                "alias": "reviewer",
                "name": "Reviewer",
                "description": "Reviews evidence and proposed changes.",
                "instructions": "Report concrete defects and verify the requested outcome.",
                "model_target": "local-fast",
                "thinking": "off",
                "permission": "read_only",
            },
            {
                "alias": "api_expert",
                "name": "API Expert",
                "description": "Checks the API boundary when asked.",
                "instructions": "Answer focused API compatibility questions.",
                "model_target": "inherit",
                "thinking": "inherit",
                "permission": "read_only",
            },
        ],
        "occurrences": [
            {
                "alias": "research",
                "agent_ref": "investigator",
                "assignment": "Find the root cause and relevant constraints.",
            },
            {
                "alias": "implementation",
                "agent_ref": "implementer",
                "assignment": "Implement the core fix.",
            },
            {
                "alias": "compatibility",
                "agent_ref": "migrator",
                "assignment": "Update the compatibility path in parallel.",
            },
            {
                "alias": "review",
                "agent_ref": "reviewer",
                "assignment": "Review both completed branches together.",
            },
            {
                "alias": "api_help",
                "agent_ref": "api_expert",
                "assignment": "Answer API questions if investigation needs help.",
            },
            {
                "alias": "nested_review",
                "agent_ref": "reviewer",
                "assignment": "Double-check the helper's answer if asked.",
            },
        ],
        "handoffs": [
            {"source": "task", "target": "research"},
            {"source": "research", "target": "implementation"},
            {"source": "research", "target": "compatibility"},
            {"source": "implementation", "target": "review"},
            {"source": "compatibility", "target": "review"},
            {"source": "review", "target": "result"},
        ],
        "helpers": [
            {"parent": "research", "helper": "api_help"},
            {"parent": "api_help", "helper": "nested_review"},
        ],
    }


def test_schema_is_flat_strict_and_requires_the_complete_contract() -> None:
    tool = build_run_agent_team_tool_def()
    parameters = _parameters(tool)

    assert tool["type"] == "function"
    assert tool["function"]["name"] == "run_agent_team"
    assert parameters["required"] == [
        "task",
        "team_name",
        "team_description",
        "new_agents",
        "occurrences",
        "handoffs",
        "helpers",
    ]
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["occurrences"]["minItems"] == 1
    assert (
        parameters["properties"]["occurrences"]["maxItems"]
        == MAX_AGENT_TEAM_OCCURRENCES
    )
    for array_name in ("new_agents", "occurrences", "handoffs", "helpers"):
        assert parameters["properties"][array_name]["items"][
            "additionalProperties"
        ] is False

    for item in _walk(parameters):
        if isinstance(item, dict):
            assert "$ref" not in item
            assert "$defs" not in item
            assert "oneOf" not in item
            assert "anyOf" not in item


def test_builder_projects_only_safe_existing_agent_rows_and_frozen_targets() -> None:
    tool = build_run_agent_team_tool_def(
        existing_agents=[
            {
                "agent_id": "reviewer000",
                "name": "Reviewer",
                "description": "Reviews changes for defects.",
                "permission_label": "Read only",
                "instructions": "SECRET CHILD BRIEF",
            }
        ],
        model_targets=[
            {"key": "local-fast", "label": "Local fast model"},
            "hosted-strong",
            {"target_key": "local-fast", "label": "duplicate"},
            "inherit",
        ],
    )
    rendered = str(tool)
    model_target = _parameters(tool)["properties"]["new_agents"]["items"][
        "properties"
    ]["model_target"]

    assert "reviewer000" in rendered
    assert "Reviewer" in rendered
    assert "Reviews changes for defects." in rendered
    assert "SECRET CHILD BRIEF" not in rendered
    assert model_target["enum"] == ["inherit", "local-fast", "hosted-strong"]


def test_full_native_team_shape_validates_without_recursive_schema_features() -> None:
    tool = build_run_agent_team_tool_def(
        model_targets=("local-fast", "hosted-strong")
    )

    jsonschema.Draft202012Validator(_parameters(tool)).validate(_valid_team())

    description = tool["function"]["description"]
    assert "Use Aura directly for ordinary work" in description
    assert "Use exactly one occurrence" in description
    assert "two or more occurrences only" in description
    assert "materially improve the result" in description
    assert "outgoing handoffs branch" in description
    assert "incoming handoffs wait and join" in description
    assert "Reuse one Agent in multiple occurrences" in description
    assert "may have helpers of its own" in description
    assert "Read / Write specialists use Aura's existing isolation" in description


def test_occurrence_limit_and_required_arrays_are_enforced_by_preflight_schema() -> None:
    schema = _parameters(build_run_agent_team_tool_def(model_targets=("local-fast",)))
    validator = jsonschema.Draft202012Validator(schema)

    empty = _valid_team()
    empty["occurrences"] = []
    assert any(
        list(error.path) == ["occurrences"] for error in validator.iter_errors(empty)
    )

    too_many = _valid_team()
    too_many["occurrences"] = [
        *too_many["occurrences"],
        {
            "alias": "seventh",
            "agent_ref": "reviewer",
            "assignment": "This occurrence exceeds the autonomous allowance.",
        },
    ]
    assert any(
        list(error.path) == ["occurrences"]
        for error in validator.iter_errors(too_many)
    )

    missing_helpers = _valid_team()
    del missing_helpers["helpers"]
    assert any(error.validator == "required" for error in validator.iter_errors(missing_helpers))


def test_empty_new_agent_and_helper_arrays_are_valid_when_reusing_an_agent() -> None:
    tool = build_run_agent_team_tool_def(
        existing_agents=(
            {
                "agent_id": "reviewer000",
                "name": "Reviewer",
                "description": "Reviews the requested work.",
                "permission_label": "Read only",
            },
        )
    )
    args = {
        "task": "Review the current implementation.",
        "team_name": "Review team",
        "team_description": "Use the existing specialist once.",
        "new_agents": [],
        "occurrences": [
            {
                "alias": "review",
                "agent_ref": "reviewer000",
                "assignment": "Inspect the implementation and report defects.",
            }
        ],
        "handoffs": [
            {"source": "task", "target": "review"},
            {"source": "review", "target": "result"},
        ],
        "helpers": [],
    }

    jsonschema.Draft202012Validator(_parameters(tool)).validate(args)
    assert _parameters(tool)["properties"]["new_agents"]["items"]["properties"][
        "model_target"
    ]["enum"] == ["inherit"]
