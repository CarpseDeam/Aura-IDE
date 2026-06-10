"""Tests for aura.drones.store — DroneStore persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from aura.drones.capabilities import CapabilityBinding, CapabilityRequirement
from aura.drones.definition import DroneBudget, DroneDefinition, default_tools_for_policy, slugify
from aura.drones.store import DroneStore, _drone_from_dict

# ---------------------------------------------------------------------------
# list / load / save / delete
# ---------------------------------------------------------------------------


def test_list_drones_empty(tmp_path: Path) -> None:
    assert DroneStore.list_drones(tmp_path) == []
    assert not (tmp_path / ".aura" / "drones").exists()


def test_save_and_load_drone(tmp_path: Path) -> None:
    drone = DroneDefinition(
        id="test-1",
        name="Test Drone",
        description="A test drone",
        instructions="Do the thing",
        write_policy="read_only",
        allowed_tools=default_tools_for_policy("read_only"),
        output_contract="A summary",
    )
    DroneStore.save_drone(tmp_path, drone)
    loaded = DroneStore.load_drone(tmp_path, "test-1")
    assert loaded is not None
    assert loaded.id == "test-1"
    assert loaded.name == "Test Drone"
    assert loaded.description == "A test drone"
    assert loaded.instructions == "Do the thing"
    assert loaded.write_policy == "read_only"
    assert loaded.allowed_tools == drone.allowed_tools
    assert loaded.output_contract == "A summary"
    assert loaded.budget.max_tool_rounds == 8
    assert loaded.budget.timeout_seconds == 300


def test_list_drones(tmp_path: Path) -> None:
    for i in range(3):
        drone = DroneDefinition(
            id=f"drone-{i}",
            name=f"Drone {i}",
            description=f"Description {i}",
            instructions=f"Instructions {i}",
            write_policy="read_only",
            allowed_tools=default_tools_for_policy("read_only"),
            output_contract=f"Output {i}",
        )
        DroneStore.save_drone(tmp_path, drone)

    drones = DroneStore.list_drones(tmp_path)
    assert len(drones) == 3
    assert {d.id for d in drones} == {"drone-0", "drone-1", "drone-2"}


def test_save_creates_directory(tmp_path: Path) -> None:
    drone = DroneDefinition(
        id="first",
        name="First",
        description="",
        instructions="Do the first task",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="Return a summary",
    )
    drone_dir = tmp_path / ".aura" / "drones"
    assert not drone_dir.exists()
    DroneStore.save_drone(tmp_path, drone)
    assert drone_dir.exists()


def test_save_rejects_missing_required_fields(tmp_path: Path) -> None:
    drone = DroneDefinition(
        id="missing-fields",
        name="Missing Fields",
        description="",
        instructions="",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="",
    )

    with pytest.raises(ValueError, match="instructions"):
        DroneStore.save_drone(tmp_path, drone)


def test_delete_drone(tmp_path: Path) -> None:
    drone = DroneDefinition(
        id="to-delete",
        name="Delete Me",
        description="",
        instructions="Delete this test drone",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="Return a summary",
    )
    DroneStore.save_drone(tmp_path, drone)
    assert DroneStore.load_drone(tmp_path, "to-delete") is not None

    deleted = DroneStore.delete_drone(tmp_path, "to-delete")
    assert deleted is True
    assert DroneStore.load_drone(tmp_path, "to-delete") is None
    assert DroneStore.list_drones(tmp_path) == []


def test_delete_nonexistent(tmp_path: Path) -> None:
    assert DroneStore.delete_drone(tmp_path, "does-not-exist") is False


def test_save_updates_existing(tmp_path: Path) -> None:
    drone = DroneDefinition(
        id="update-me",
        name="Original",
        description="Original description",
        instructions="Original instructions",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="Original output",
    )
    DroneStore.save_drone(tmp_path, drone)

    updated = DroneDefinition(
        id="update-me",
        name="Updated",
        description="Updated description",
        instructions="Updated instructions",
        write_policy="ask_before_writes",
        allowed_tools=default_tools_for_policy("ask_before_writes"),
        output_contract="Updated output",
    )
    DroneStore.save_drone(tmp_path, updated)

    loaded = DroneStore.load_drone(tmp_path, "update-me")
    assert loaded is not None
    assert loaded.name == "Updated"
    assert loaded.description == "Updated description"
    assert loaded.write_policy == "ask_before_writes"


def test_invalid_json_skipped(tmp_path: Path) -> None:
    drones_dir = DroneStore.drones_dir(tmp_path)
    # Write a valid drone
    drone = DroneDefinition(
        id="valid",
        name="Valid",
        description="",
        instructions="Inspect the project",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="Return a summary",
    )
    DroneStore.save_drone(tmp_path, drone)

    # Write an invalid json file
    (drones_dir / "bad.json").write_text("{{{not json", encoding="utf-8")

    drones = DroneStore.list_drones(tmp_path)
    assert len(drones) == 1
    assert drones[0].id == "valid"


# ---------------------------------------------------------------------------
# next_id
# ---------------------------------------------------------------------------


def test_next_id_basic(tmp_path: Path) -> None:
    assert DroneStore.next_id(tmp_path, "Release Check") == "release-check"


def test_next_id_duplicate(tmp_path: Path) -> None:
    drone = DroneDefinition(
        id="release-check",
        name="Release Check",
        description="",
        instructions="Check the release",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="Return a summary",
    )
    DroneStore.save_drone(tmp_path, drone)
    assert DroneStore.next_id(tmp_path, "Release Check") == "release-check-1"


def test_next_id_multiple_duplicates(tmp_path: Path) -> None:
    for i in range(4):
        drone = DroneDefinition(
            id=f"my-drone-{i}" if i > 0 else "my-drone",
            name=f"My Drone {i}",
            description="",
            instructions="Inspect the project",
            write_policy="read_only",
            allowed_tools=(),
            output_contract="Return a summary",
        )
        DroneStore.save_drone(tmp_path, drone)

    assert DroneStore.next_id(tmp_path, "My Drone") == "my-drone-4"


# ---------------------------------------------------------------------------
# defaults & helpers
# ---------------------------------------------------------------------------


def test_drone_budget_defaults() -> None:
    b = DroneBudget()
    assert b.max_tool_rounds == 8
    assert b.timeout_seconds == 300


def test_drone_definition_defaults() -> None:
    d = DroneDefinition(
        id="defaults-test",
        name="Defaults",
        description="",
        instructions="",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="",
    )
    assert d.scope == "project"
    assert d.enabled is True
    assert d.created_by == "user"
    assert d.created_at == ""
    assert d.updated_at == ""
    assert isinstance(d.budget, DroneBudget)
    assert d.budget.max_tool_rounds == 8


def test_default_tools_for_policy_read_only() -> None:
    tools = default_tools_for_policy("read_only")
    assert "read_file" in tools
    assert "read_files" in tools
    assert "list_directory" in tools
    assert "glob" in tools
    assert "grep_search" in tools
    assert "read_file_outline" in tools
    assert "find_usages" in tools
    assert "search_codebase" in tools
    assert "git_status" in tools
    assert "git_diff" in tools
    assert "git_log" in tools
    assert "git_show" in tools
    assert "git_log_file" in tools
    assert "git_branch_list" in tools
    assert "git_stash_list" in tools
    assert "git_stash_show" in tools
    assert "run_terminal_command" in tools
    assert "run_diagnostic_command" in tools
    assert "get_workspace_snapshot" in tools
    assert len(tools) == 19


def test_default_tools_for_policy_unknown() -> None:
    tools = default_tools_for_policy("nonexistent_policy")
    assert len(tools) == 19


def test_slugify() -> None:
    assert slugify("Release Check") == "release-check"
    assert slugify("Hello World!") == "hello-world"
    assert slugify("  spaces  ") == "spaces"
    assert slugify("a---b") == "a-b"
    assert slugify("---") == ""


# ---------------------------------------------------------------------------
# new field backward compat & roundtrip (capability routing fields)
# ---------------------------------------------------------------------------


def test_drone_from_dict_old_json_backward_compat() -> None:
    """Old JSON without capability fields loads with defaults."""
    old_data: dict = {
        "id": "legacy",
        "name": "Legacy",
        "description": "",
        "instructions": "Do the thing",
        "write_policy": "read_only",
        "allowed_tools": ["read_file", "grep_search"],
        "output_contract": "A summary",
    }
    drone = _drone_from_dict(old_data)
    assert drone.capability_requirements == ()
    assert drone.capability_bindings == ()
    assert drone.setup_steps == ()
    assert drone.first_run_test == ""


def test_drone_new_fields_roundtrip(tmp_path: Path) -> None:
    """DroneDefinition with capability fields survives save/load."""
    req = CapabilityRequirement(capability="web_search", purpose="Find docs")
    bind = CapabilityBinding(
        capability="web_search",
        route_kind="api",
        source="tavily",
        tool_names=("tavily_search",),
        setup_status="installed",
    )
    drone = DroneDefinition(
        id="cap-roundtrip",
        name="Cap Roundtrip",
        description="",
        instructions="Search the web",
        write_policy="read_only",
        allowed_tools=("tavily_search",),
        output_contract="Search results",
        capability_requirements=(req,),
        capability_bindings=(bind,),
        setup_steps=("ensure api key", "verify endpoint"),
        first_run_test="tavily_search --query test",
    )
    DroneStore.save_drone(tmp_path, drone)
    loaded = DroneStore.load_drone(tmp_path, "cap-roundtrip")
    assert loaded is not None
    assert loaded.capability_requirements == (req,)
    assert loaded.capability_bindings == (bind,)
    assert loaded.setup_steps == ("ensure api key", "verify endpoint")
    assert loaded.first_run_test == "tavily_search --query test"


def test_drone_from_dict_setup_steps_list_to_tuple() -> None:
    """setup_steps list in JSON is converted to tuple."""
    data: dict = {
        "id": "setup-test",
        "name": "Setup Test",
        "description": "",
        "instructions": "Do setup",
        "write_policy": "read_only",
        "allowed_tools": [],
        "output_contract": "OK",
        "setup_steps": ["step1", "step2"],
    }
    drone = _drone_from_dict(data)
    assert isinstance(drone.setup_steps, tuple)
    assert drone.setup_steps == ("step1", "step2")


def test_drone_first_run_test_roundtrip(tmp_path: Path) -> None:
    """first_run_test field survives save/load."""
    drone = DroneDefinition(
        id="first-run-test",
        name="First Run",
        description="",
        instructions="Run a test",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="Result",
        first_run_test="echo hello",
    )
    DroneStore.save_drone(tmp_path, drone)
    loaded = DroneStore.load_drone(tmp_path, "first-run-test")
    assert loaded is not None
    assert loaded.first_run_test == "echo hello"


def test_drone_new_field_defaults() -> None:
    """DroneDefinition created without new fields gets correct defaults."""
    drone = DroneDefinition(
        id="defaults-new",
        name="Defaults New",
        description="",
        instructions="",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="",
    )
    assert drone.capability_requirements == ()
    assert drone.capability_bindings == ()
    assert drone.setup_steps == ()
    assert drone.first_run_test == ""


def test_editor_simulated_update_preserves_first_run_test(tmp_path: Path) -> None:
    """Simulate editor save: construct new DroneDefinition with updated metadata
    but the same first_run_test, proving the store roundtrip preserves the field."""
    original = DroneDefinition(
        id="editor-test",
        name="Original",
        description="Original desc",
        instructions="Original instructions",
        write_policy="read_only",
        allowed_tools=default_tools_for_policy("read_only"),
        output_contract="Original output",
        first_run_test="run --smoke",
    )
    DroneStore.save_drone(tmp_path, original)

    # Simulate editor save: new name/instructions/policy, same first_run_test
    updated = DroneDefinition(
        id="editor-test",
        name="Updated Name",
        description="Updated desc",
        instructions="Updated instructions",
        write_policy="ask_before_writes",
        allowed_tools=default_tools_for_policy("ask_before_writes"),
        output_contract="Updated output",
        first_run_test="run --smoke",
    )
    DroneStore.save_drone(tmp_path, updated)

    loaded = DroneStore.load_drone(tmp_path, "editor-test")
    assert loaded is not None
    assert loaded.name == "Updated Name"
    assert loaded.first_run_test == "run --smoke"
