"""Tests for aura.drones.chain — chain data model, validation, and store."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from aura import paths as aura_paths
from aura.drones.chain import (
    ChainDefinition,
    ChainEdge,
    ChainGoal,
    ChainNode,
    ChainValidation,
    topological_order,
    validate,
)
from aura.drones.chain_store import ChainStore, _chain_from_dict, _normalize_chain_data
from aura.drones.definition import DroneDefinition
# ── Helpers ─────────────────────────────────────────────────────────


def _make_drone(
    drone_id: str,
    name: str = "",
    accepts: str = "",
    produces: str = "",
    enabled: bool = True,
) -> DroneDefinition:
    return DroneDefinition(
        id=drone_id,
        name=name or drone_id,
        description="",
        instructions="Do the thing",
        write_policy="read_only",
        allowed_tools=(),
        output_contract="Output",
        enabled=enabled,
        accepts=accepts,
        produces=produces,
    )


# ── Data model round-trip ──────────────────────────────────────────


def test_chain_definition_roundtrip() -> None:
    """Create a ChainDefinition, serialize via asdict, reconstruct, assert equal."""
    chain = ChainDefinition(
        id="test-chain",
        name="Test Chain",
        description="A test chain for round-trip",
        nodes=(
            ChainNode(
                id="n1",
                drone_id="drone-a",
                goal_template="search {{query}}",
                position=(100.0, 200.0),
            ),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
        created_at="2025-01-01T00:00:00",
        updated_at="2025-01-02T00:00:00",
        schedule="0 0 * * *",
    )
    data = asdict(chain)
    reconstructed = _chain_from_dict(data)
    assert reconstructed == chain


def test_chain_definition_empty_nodes_edges() -> None:
    """A chain with no nodes or edges round-trips correctly."""
    chain = ChainDefinition(
        id="empty",
        name="Empty",
        description="An empty chain",
    )
    data = asdict(chain)
    reconstructed = _chain_from_dict(data)
    assert reconstructed == chain
    assert reconstructed.nodes == ()
    assert reconstructed.edges == ()


def test_chain_definition_default_fields() -> None:
    """Default field values are set correctly."""
    chain = ChainDefinition(
        id="defaults",
        name="Defaults",
        description="Default field chain",
    )
    assert chain.nodes == ()
    assert chain.edges == ()
    assert chain.created_at == ""
    assert chain.updated_at == ""
    assert chain.enabled is True
    assert chain.schedule == ""


# ── ChainValidation ────────────────────────────────────────────────


def test_chain_validation_fresh() -> None:
    """A fresh ChainValidation has ok=False and empty errors."""
    v = ChainValidation()
    assert v.ok is False
    assert v.errors == []


def test_chain_validation_with_errors() -> None:
    """ChainValidation with errors has ok=False."""
    v = ChainValidation(ok=False, errors=["error 1"])
    assert v.ok is False
    assert v.errors == ["error 1"]


def test_chain_validation_ok() -> None:
    """ChainValidation with ok=True and no errors."""
    v = ChainValidation(ok=True)
    assert v.ok is True
    assert v.errors == []


# ── topological_order ──────────────────────────────────────────────


def test_topological_order_simple() -> None:
    """A linear chain produces ids in order."""
    chain = ChainDefinition(
        id="linear",
        name="Linear",
        description="",
        nodes=(
            ChainNode(id="a", drone_id="d1"),
            ChainNode(id="b", drone_id="d2"),
            ChainNode(id="c", drone_id="d3"),
        ),
        edges=(
            ChainEdge(from_node="a", to_node="b"),
            ChainEdge(from_node="b", to_node="c"),
        ),
    )
    order = topological_order(chain)
    assert order == ["a", "b", "c"]


def test_topological_order_branching() -> None:
    """A DAG with branches produces a valid topological order."""
    chain = ChainDefinition(
        id="branching",
        name="Branching",
        description="",
        nodes=(
            ChainNode(id="a", drone_id="d1"),
            ChainNode(id="b", drone_id="d2"),
            ChainNode(id="c", drone_id="d3"),
        ),
        edges=(
            ChainEdge(from_node="a", to_node="b"),
            ChainEdge(from_node="a", to_node="c"),
        ),
    )
    order = topological_order(chain)
    assert order[0] == "a"
    assert set(order[1:]) == {"b", "c"}


def test_topological_order_disconnected() -> None:
    """Disconnected sub-graphs are topologically orderable."""
    chain = ChainDefinition(
        id="disconnected",
        name="Disconnected",
        description="",
        nodes=(
            ChainNode(id="a", drone_id="d1"),
            ChainNode(id="b", drone_id="d2"),
            ChainNode(id="c", drone_id="d3"),
        ),
        edges=(
            ChainEdge(from_node="a", to_node="b"),
        ),
    )
    order = topological_order(chain)
    assert order[0] == "a" or order[0] == "c"
    assert len(order) == 3


def test_topological_order_cycle() -> None:
    """A cycle raises ValueError."""
    chain = ChainDefinition(
        id="cycle",
        name="Cycle",
        description="",
        nodes=(
            ChainNode(id="a", drone_id="d1"),
            ChainNode(id="b", drone_id="d2"),
            ChainNode(id="c", drone_id="d3"),
        ),
        edges=(
            ChainEdge(from_node="a", to_node="b"),
            ChainEdge(from_node="b", to_node="c"),
            ChainEdge(from_node="c", to_node="a"),
        ),
    )
    with pytest.raises(ValueError, match="Cycle detected"):
        topological_order(chain)


# ── validate — passing ─────────────────────────────────────────────


def test_validate_passing() -> None:
    """A valid chain with compatible types passes."""
    drone_a = _make_drone("drone-a", produces="SearchBrief")
    drone_b = _make_drone("drone-b", accepts="SearchBrief")
    lookup = {"drone-a": drone_a, "drone-b": drone_b}

    chain = ChainDefinition(
        id="passing",
        name="Passing Chain",
        description="Passes validation",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is True
    assert result.errors == []


def test_validate_passing_single_node() -> None:
    """A single-node chain (no edges) passes validation."""
    drone = _make_drone("drone-a")
    lookup = {"drone-a": drone}

    chain = ChainDefinition(
        id="single",
        name="Single Node",
        description="Just one node",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )

    result = validate(chain, lookup)
    assert result.ok is True


# ── validate — no start node ───────────────────────────────────────


def test_validate_no_start_node() -> None:
    """A chain where every node has an inbound edge has no start node."""
    drone = _make_drone("drone-a")
    lookup = {"drone-a": drone}

    chain = ChainDefinition(
        id="no-start",
        name="No Start",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-a"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
            ChainEdge(from_node="n2", to_node="n1"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is False
    assert any("no start node" in e.lower() for e in result.errors)


# ── validate — missing drone_id ────────────────────────────────────


def test_validate_missing_drone_id() -> None:
    """A node referencing a drone not in lookup fails."""
    drone_b = _make_drone("drone-b")
    lookup = {"drone-b": drone_b}

    chain = ChainDefinition(
        id="missing",
        name="Missing Drone",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is False
    assert any("drone-a" in e for e in result.errors)
    assert any("n1" in e for e in result.errors)


# ── validate — disabled drone ──────────────────────────────────────


def test_validate_disabled_drone() -> None:
    """A node referencing a disabled drone fails."""
    drone_a = _make_drone("drone-a", enabled=False)
    drone_b = _make_drone("drone-b")
    lookup = {"drone-a": drone_a, "drone-b": drone_b}

    chain = ChainDefinition(
        id="disabled",
        name="Disabled Drone",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is False
    assert any("disabled" in e.lower() for e in result.errors)


# ── validate — cycle ───────────────────────────────────────────────


def test_validate_cycle() -> None:
    """A chain with a cycle fails validation."""
    drone = _make_drone("drone-a")
    lookup = {"drone-a": drone}

    chain = ChainDefinition(
        id="cycle-chain",
        name="Cycle Chain",
        description="",
        nodes=(
            ChainNode(id="a", drone_id="drone-a"),
            ChainNode(id="b", drone_id="drone-a"),
            ChainNode(id="c", drone_id="drone-a"),
        ),
        edges=(
            ChainEdge(from_node="a", to_node="b"),
            ChainEdge(from_node="b", to_node="c"),
            ChainEdge(from_node="c", to_node="a"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is False
    assert any("cycle" in e.lower() for e in result.errors)


# ── validate — type incompatible ───────────────────────────────────


def test_validate_type_incompatible() -> None:
    """Edge with incompatible types fails with a message naming both types."""
    drone_a = _make_drone("drone-a", produces="SearchBrief")
    drone_b = _make_drone("drone-b", accepts="FitReview")
    lookup = {"drone-a": drone_a, "drone-b": drone_b}

    chain = ChainDefinition(
        id="type-mismatch",
        name="Type Mismatch",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is False
    type_errors = [e for e in result.errors if "type mismatch" in e]
    assert len(type_errors) >= 1
    assert "SearchBrief" in type_errors[0]
    assert "FitReview" in type_errors[0]


# ── validate — free-form interop ───────────────────────────────────


def test_validate_free_form_both_empty() -> None:
    """Both producer and consumer free-form passes."""
    drone_a = _make_drone("drone-a", produces="")
    drone_b = _make_drone("drone-b", accepts="")
    lookup = {"drone-a": drone_a, "drone-b": drone_b}

    chain = ChainDefinition(
        id="free-both",
        name="Free Both",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is True


def test_validate_free_form_producer_typed_consumer_free() -> None:
    """Producer with typed output, consumer free-form — passes."""
    drone_a = _make_drone("drone-a", produces="SearchBrief")
    drone_b = _make_drone("drone-b", accepts="")
    lookup = {"drone-a": drone_a, "drone-b": drone_b}

    chain = ChainDefinition(
        id="typed-to-free",
        name="Typed to Free",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is True


def test_validate_free_form_producer_free_consumer_typed() -> None:
    """Producer free-form, consumer requires typed — fails."""
    drone_a = _make_drone("drone-a", produces="")
    drone_b = _make_drone("drone-b", accepts="OpportunityBatch")
    lookup = {"drone-a": drone_a, "drone-b": drone_b}

    chain = ChainDefinition(
        id="free-to-typed",
        name="Free to Typed",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is False
    assert any("no produces type" in e for e in result.errors)


# ── ChainStore round-trip ──────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point data_dir to a tmp_path subdirectory for test isolation."""
    monkeypatch.setattr(aura_paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("aura.drones.chain_store.data_dir", lambda: tmp_path / "data")


def test_chain_store_save_and_load(tmp_path: Path) -> None:
    """Save a chain, load it back, assert equality."""
    chain = ChainDefinition(
        id="my-chain",
        name="My Chain",
        description="A test chain",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    ChainStore.save_chain(tmp_path, chain)
    loaded = ChainStore.load_chain(tmp_path, "my-chain")
    assert loaded is not None
    assert loaded == chain


def test_chain_store_list_chains(tmp_path: Path) -> None:
    """List returns saved chains sorted by name."""
    chain_b = ChainDefinition(
        id="chain-b", name="Beta", description="Second chain"
    )
    chain_a = ChainDefinition(
        id="chain-a", name="Alpha", description="First chain"
    )
    ChainStore.save_chain(tmp_path, chain_b)
    ChainStore.save_chain(tmp_path, chain_a)

    chains = ChainStore.list_chains(tmp_path)
    assert len(chains) == 2
    assert chains[0].name == "Alpha"
    assert chains[1].name == "Beta"


def test_chain_store_list_empty(tmp_path: Path) -> None:
    """list_chains returns empty list when no chains exist."""
    assert ChainStore.list_chains(tmp_path) == []


def test_chain_store_delete(tmp_path: Path) -> None:
    """Delete a chain, verify it's gone."""
    chain = ChainDefinition(
        id="to-delete",
        name="Delete Me",
        description="Will be deleted",
    )
    ChainStore.save_chain(tmp_path, chain)
    assert ChainStore.load_chain(tmp_path, "to-delete") is not None

    deleted = ChainStore.delete_chain(tmp_path, "to-delete")
    assert deleted is True
    assert ChainStore.load_chain(tmp_path, "to-delete") is None
    assert ChainStore.list_chains(tmp_path) == []


def test_chain_store_delete_nonexistent(tmp_path: Path) -> None:
    """Deleting a nonexistent chain returns False."""
    assert ChainStore.delete_chain(tmp_path, "does-not-exist") is False


def test_chain_store_save_creates_directory(tmp_path: Path) -> None:
    """Saving a chain creates the chain directory and chain.json."""
    chain = ChainDefinition(
        id="first-chain",
        name="First Chain",
        description="First chain for testing",
    )
    ChainStore.save_chain(tmp_path, chain)

    chain_dir = ChainStore.chains_dir() / "first-chain"
    assert chain_dir.exists()
    assert (chain_dir / "chain.json").exists()


def test_chain_store_save_updates_existing(tmp_path: Path) -> None:
    """Saving with the same id overwrites the existing chain."""
    original = ChainDefinition(
        id="update-me",
        name="Original",
        description="Original description",
    )
    ChainStore.save_chain(tmp_path, original)

    updated = ChainDefinition(
        id="update-me",
        name="Updated",
        description="Updated description",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )
    ChainStore.save_chain(tmp_path, updated)

    loaded = ChainStore.load_chain(tmp_path, "update-me")
    assert loaded is not None
    assert loaded.name == "Updated"
    assert loaded.description == "Updated description"
    assert len(loaded.nodes) == 1


def test_chain_store_load_nonexistent(tmp_path: Path) -> None:
    """load_chain returns None for nonexistent chains."""
    assert ChainStore.load_chain(tmp_path, "no-such-chain") is None


def test_chain_store_load_invalid_id(tmp_path: Path) -> None:
    """load_chain returns None for invalid chain ids."""
    assert ChainStore.load_chain(tmp_path, "../evil") is None


# ── ChainStore next_id ─────────────────────────────────────────────


def test_chain_store_next_id_basic(tmp_path: Path) -> None:
    """next_id generates a slug from the name."""
    assert ChainStore.next_id(tmp_path, "Release Chain") == "release-chain"


def test_chain_store_next_id_duplicate(tmp_path: Path) -> None:
    """next_id appends -1 when the id already exists."""
    chain = ChainDefinition(
        id="release-chain",
        name="Release Chain",
        description="A release chain",
    )
    ChainStore.save_chain(tmp_path, chain)
    assert ChainStore.next_id(tmp_path, "Release Chain") == "release-chain-1"


def test_chain_store_next_id_multiple_duplicates(tmp_path: Path) -> None:
    """next_id increments correctly with multiple existing ids."""
    for i in range(4):
        cid = f"my-chain-{i}" if i > 0 else "my-chain"
        chain = ChainDefinition(
            id=cid,
            name=f"My Chain {i}",
            description=f"Chain number {i}",
        )
        ChainStore.save_chain(tmp_path, chain)

    assert ChainStore.next_id(tmp_path, "My Chain") == "my-chain-4"


# ── ChainStore validate_chain ──────────────────────────────────────


def test_validate_chain_rejects_empty_id(tmp_path: Path) -> None:
    """validate_chain raises ValueError for invalid chain id."""
    chain = ChainDefinition(
        id="",
        name="Test",
        description="Test chain",
    )
    with pytest.raises(ValueError, match="Chain id must be"):
        ChainStore.validate_chain(chain)


def test_validate_chain_rejects_empty_name(tmp_path: Path) -> None:
    """validate_chain raises ValueError for empty name."""
    chain = ChainDefinition(
        id="test-chain",
        name="",
        description="Test chain",
    )
    with pytest.raises(ValueError, match="Chain name is required"):
        ChainStore.validate_chain(chain)


def test_validate_chain_rejects_empty_description(tmp_path: Path) -> None:
    """validate_chain raises ValueError for empty description."""
    chain = ChainDefinition(
        id="test-chain",
        name="Test",
        description="",
    )
    with pytest.raises(ValueError, match="Chain description is required"):
        ChainStore.validate_chain(chain)


def test_validate_chain_accepts_valid(tmp_path: Path) -> None:
    """A valid chain passes validate_chain without error."""
    chain = ChainDefinition(
        id="valid-chain",
        name="Valid Chain",
        description="A valid chain",
    )
    # Should not raise
    ChainStore.validate_chain(chain)


# ── Error message clarity ──────────────────────────────────────────


def test_validate_message_names_both_types() -> None:
    """Incompatible type error message names both types."""
    drone_a = _make_drone("drone-a", produces="SearchBrief")
    drone_b = _make_drone("drone-b", accepts="FitReview")
    lookup = {"drone-a": drone_a, "drone-b": drone_b}

    chain = ChainDefinition(
        id="msg-test",
        name="Message Test",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is False
    match = [e for e in result.errors if "type mismatch" in e]
    assert len(match) >= 1
    assert "SearchBrief" in match[0]
    assert "FitReview" in match[0]


def test_validate_message_no_produces_type() -> None:
    """Error when producer has no type but consumer requires one."""
    drone_a = _make_drone("drone-a", produces="")
    drone_b = _make_drone("drone-b", accepts="OpportunityBatch")
    lookup = {"drone-a": drone_a, "drone-b": drone_b}

    chain = ChainDefinition(
        id="no-prod",
        name="No Produces",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(
            ChainEdge(from_node="n1", to_node="n2"),
        ),
    )

    result = validate(chain, lookup)
    assert result.ok is False
    match = [e for e in result.errors if "no produces type" in e]
    assert len(match) >= 1
    assert "OpportunityBatch" in match[0]


# ── Draft nodes ────────────────────────────────────────────────────


def test_draft_fields_default() -> None:
    """ChainNode defaults is_draft=False, draft fields empty."""
    node = ChainNode(id="n1", drone_id="drone-a")
    assert node.is_draft is False
    assert node.draft_name == ""
    assert node.draft_accepts == ""
    assert node.draft_produces == ""
    assert node.draft_brief == ""


def test_roundtrip_with_draft_node() -> None:
    """Chain with a draft node saves and reloads preserving draft fields."""
    chain = ChainDefinition(
        id="draft-chain", name="Draft Chain", description="Has draft",
        nodes=(
            ChainNode(id="d1", drone_id="drone-a"),
            ChainNode(id="d2", drone_id="__draft__", is_draft=True,
                      draft_name="My Draft", draft_accepts="text",
                      draft_produces="json", draft_brief="Does stuff"),
        ),
        edges=(ChainEdge(from_node="d1", to_node="d2"),),
    )
    data = asdict(chain)
    reconstructed = _chain_from_dict(data)
    assert reconstructed == chain
    draft = [n for n in reconstructed.nodes if n.is_draft][0]
    assert draft.draft_name == "My Draft"
    assert draft.draft_accepts == "text"
    assert draft.draft_produces == "json"
    assert draft.draft_brief == "Does stuff"


def test_validate_fails_for_draft_node() -> None:
    """validate() fails when any node is_draft."""
    drone_a = _make_drone("drone-a")
    lookup = {"drone-a": drone_a}
    chain = ChainDefinition(
        id="draft-run", name="Draft Run", description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="__draft__", is_draft=True,
                      draft_name="WIP"),
        ),
        edges=(ChainEdge(from_node="n1", to_node="n2"),),
    )
    result = validate(chain, lookup)
    assert result.ok is False
    assert any("draft" in e.lower() for e in result.errors)
    assert any("n2" in e for e in result.errors)
    assert any("save it before running" in e for e in result.errors)


def test_validate_draft_error_message_exact() -> None:
    """Error message for draft node is human-readable."""
    chain = ChainDefinition(
        id="draft-msg", name="Draft Msg", description="",
        nodes=(ChainNode(id="n1", drone_id="__draft__", is_draft=True,
                         draft_name="My Draft"),),
    )
    result = validate(chain, {})
    assert result.ok is False
    match = [e for e in result.errors if "draft" in e.lower()]
    assert len(match) >= 1
    assert "n1" in match[0]
    assert "save it before running" in match[0]


def test_old_chain_loads_without_draft_fields() -> None:
    """A chain JSON without draft fields loads with defaults."""
    data = {
        "id": "old-chain", "name": "Old", "description": "Old chain",
        "nodes": [{"id": "n1", "drone_id": "drone-a", "position": [10, 20]}],
        "edges": [],
    }
    chain = _chain_from_dict(data)
    node = chain.nodes[0]
    assert node.is_draft is False
    assert node.draft_name == ""


# ── Multi-goal cleanup ─────────────────────────────────────────────


@pytest.fixture(scope="session")
def qapp():
    """Provide a QApplication instance for GoalPlanetItem tests."""
    import sys
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def test_two_goal_save_load_roundtrip(tmp_path: Path) -> None:
    """Chain with 2 ChainGoal objects round-trips via ChainStore."""
    chain = ChainDefinition(
        id="two-goals",
        name="Two Goals",
        description="Has two goals",
        goals=(
            ChainGoal(id="g1", title="Goal 1", objective="First goal"),
            ChainGoal(id="g2", title="Goal 2", objective="Second goal"),
        ),
    )
    ChainStore.save_chain(tmp_path, chain)
    loaded = ChainStore.load_chain(tmp_path, "two-goals")
    assert loaded is not None
    assert len(loaded.goals) == 2
    assert loaded.goals[0].id == "g1"
    assert loaded.goals[0].objective == "First goal"
    assert loaded.goals[1].id == "g2"
    assert loaded.goals[1].objective == "Second goal"
    assert loaded.goals[0].id != loaded.goals[1].id


def test_legacy_mission_goal_normalizes(tmp_path: Path) -> None:
    """Legacy mission_goal without goals list normalizes into goals
    and auto-assigns goal_id to untargeted assignment nodes."""
    chain_dir = ChainStore.chains_dir() / "legacy-chain"
    chain_dir.mkdir(parents=True)
    raw_data = {
        "id": "legacy-chain",
        "name": "Legacy Chain",
        "description": "A legacy chain",
        "mission_goal": "Do the legacy thing",
        "nodes": [
            {"id": "n1", "drone_id": "drone-a", "is_assignment": True,
             "goal_id": ""},
        ],
        "edges": [],
    }
    (chain_dir / "chain.json").write_text(json.dumps(raw_data))

    # Use module-level load_chain which normalizes legacy data
    from aura.drones.chain_store import load_chain as load_raw_chain
    result = load_raw_chain(tmp_path, "legacy-chain")
    assert result is not None
    assert len(result["goals"]) == 1
    assert result["goals"][0]["objective"] == "Do the legacy thing"

    # Assignment node should have been auto-assigned to the migrated goal
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["goal_id"] == f"legacy-chain-goal-default"

    # Verify _chain_from_dict constructs correctly from normalized data
    chain = _chain_from_dict(result)
    assert len(chain.goals) == 1
    assert chain.goals[0].objective == "Do the legacy thing"


# ── _chain_from_dict legacy key acceptance ─────────────────────────


def test_chain_from_dict_accepts_legacy_goal_keys() -> None:
    """_chain_from_dict accepts goal_id→id and goal→objective fallbacks."""
    data = {
        "id": "legacy-keys",
        "name": "Legacy Keys",
        "description": "",
        "goals": [
            {"goal_id": "g1", "goal": "do the thing", "title": "Goal 1", "position": [100, 200]},
        ],
    }
    chain = _chain_from_dict(data)
    assert len(chain.goals) == 1
    assert chain.goals[0].id == "g1"
    assert chain.goals[0].objective == "do the thing"
    assert chain.goals[0].title == "Goal 1"
    assert chain.goals[0].position == (100.0, 200.0)


def test_chain_from_dict_accepts_mixed_old_new() -> None:
    """_chain_from_dict handles one legacy and one modern goal dict."""
    data = {
        "id": "mixed",
        "name": "Mixed",
        "description": "",
        "goals": [
            {"goal_id": "g1", "goal": "old way", "title": "Old", "position": [0, 0]},
            {"id": "g2", "objective": "new way", "title": "New", "position": [100, 0]},
        ],
    }
    chain = _chain_from_dict(data)
    assert len(chain.goals) == 2
    assert chain.goals[0].id == "g1"
    assert chain.goals[0].objective == "old way"
    assert chain.goals[1].id == "g2"
    assert chain.goals[1].objective == "new way"


def test_chain_from_dict_canonical_preferred() -> None:
    """When both canonical and legacy keys exist, canonical wins."""
    data = {
        "id": "both-keys",
        "name": "Both Keys",
        "description": "",
        "goals": [
            {
                "id": "canonical-id",
                "goal_id": "legacy-id",
                "objective": "canonical objective",
                "goal": "legacy objective",
                "title": "Test",
            },
        ],
    }
    chain = _chain_from_dict(data)
    assert len(chain.goals) == 1
    assert chain.goals[0].id == "canonical-id"
    assert chain.goals[0].objective == "canonical objective"


# ── Module-level load_chain goal_id auto-assignment ────────────────


def test_load_chain_does_not_autoassign_blank_goal_id_in_multigoal(tmp_path: Path) -> None:
    """Modern multi-goal chain: assignment nodes with blank goal_id stay blank."""
    chain_dir = ChainStore.chains_dir() / "multi-goal-chain"
    chain_dir.mkdir(parents=True)
    raw_data = {
        "id": "multi-goal-chain",
        "name": "Multi Goal",
        "description": "",
        "goals": [
            {"id": "g1", "title": "Goal 1", "objective": "First"},
            {"id": "g2", "title": "Goal 2", "objective": "Second"},
        ],
        "nodes": [
            {"id": "n1", "drone_id": "drone-a", "is_assignment": True, "goal_id": ""},
        ],
        "edges": [],
    }
    (chain_dir / "chain.json").write_text(json.dumps(raw_data))

    from aura.drones.chain_store import load_chain as load_raw_chain
    result = load_raw_chain(tmp_path, "multi-goal-chain")
    assert result is not None
    assert len(result["nodes"]) == 1
    # goal_id should remain empty — not auto-assigned
    assert result["nodes"][0]["goal_id"] == ""


def test_load_chain_autoassigns_for_legacy_single_goal(tmp_path: Path) -> None:
    """Legacy single-goal chain auto-assigns goal_id to untargeted assignment nodes."""
    chain_dir = ChainStore.chains_dir() / "legacy-assign-chain"
    chain_dir.mkdir(parents=True)
    raw_data = {
        "id": "legacy-assign-chain",
        "name": "Legacy Assign",
        "description": "",
        "mission_goal": "old goal",
        "nodes": [
            {"id": "n1", "drone_id": "drone-a", "is_assignment": True, "goal_id": ""},
        ],
        "edges": [],
    }
    (chain_dir / "chain.json").write_text(json.dumps(raw_data))

    from aura.drones.chain_store import load_chain as load_raw_chain
    result = load_raw_chain(tmp_path, "legacy-assign-chain")
    assert result is not None
    assert len(result["goals"]) == 1
    # The auto-assigned goal id
    expected_goal_id = f"legacy-assign-chain-goal-default"
    assert result["goals"][0]["id"] == expected_goal_id
    # The assignment node should now reference that goal
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["goal_id"] == expected_goal_id


def test_goalplanet_item_to_dict_uses_canonical_keys(qapp) -> None:
    """GoalPlanetItem.to_dict returns id and objective keys, not goal_id and goal."""
    from unittest.mock import MagicMock
    from aura.gui.drones.chain_canvas import GoalPlanetItem

    mock_canvas = MagicMock()
    gp = GoalPlanetItem(node_id="test-gp", canvas=mock_canvas, goal_id="goal-1")
    gp._objective = "Test objective"
    gp._title = "Test Title"

    data = gp.to_dict()
    assert "id" in data
    assert "objective" in data
    assert "goal_id" not in data
    assert "goal" not in data
    assert data["id"] == "goal-1"
    assert data["objective"] == "Test objective"
    assert data["title"] == "Test Title"


def test_goalplanet_item_from_dict_accepts_both_shapes(qapp) -> None:
    """GoalPlanetItem.from_dict accepts both old (goal_id/goal) and new (id/objective) keys."""
    from unittest.mock import MagicMock
    from aura.gui.drones.chain_canvas import GoalPlanetItem

    mock_canvas = MagicMock()

    # Old keys
    gp = GoalPlanetItem(node_id="test-1", canvas=mock_canvas)
    gp.from_dict({"goal_id": "g1", "goal": "Old objective", "title": "Old"})
    assert gp.goal_id == "g1"
    assert gp.objective == "Old objective"
    assert gp._title == "Old"

    # New keys
    gp2 = GoalPlanetItem(node_id="test-2", canvas=mock_canvas)
    gp2.from_dict({"id": "g2", "objective": "New objective", "title": "New"})
    assert gp2.goal_id == "g2"
    assert gp2.objective == "New objective"
    assert gp2._title == "New"


def test_assignment_goal_ids_survive_save_load(tmp_path: Path) -> None:
    """Assignment nodes with goal_ids preserve those goal_ids through save/load."""
    chain = ChainDefinition(
        id="assign-goals",
        name="Assign Goals",
        description="Has assignment nodes with goal_ids",
        goals=(
            ChainGoal(id="g1", title="Goal 1", objective="First"),
            ChainGoal(id="g2", title="Goal 2", objective="Second"),
        ),
        nodes=(
            ChainNode(id="n1", drone_id="drone-a", is_assignment=True, goal_id="g1"),
            ChainNode(id="n2", drone_id="drone-b", is_assignment=True, goal_id="g2"),
        ),
        edges=(),
    )
    ChainStore.save_chain(tmp_path, chain)
    loaded = ChainStore.load_chain(tmp_path, "assign-goals")
    assert loaded is not None
    assert len(loaded.nodes) == 2
    n1 = [n for n in loaded.nodes if n.id == "n1"][0]
    n2 = [n for n in loaded.nodes if n.id == "n2"][0]
    assert n1.goal_id == "g1"
    assert n2.goal_id == "g2"


def test_load_chain_no_goal_planet_key(tmp_path: Path) -> None:
    """Module-level save_chain does not write goal_planet key."""
    from aura.drones.chain_store import save_chain as dict_save

    data = {
        "id": "no-gp",
        "name": "No GP",
        "description": "No GP test",
        "goals": [
            {"id": "g1", "title": "G1", "objective": "O1"},
            {"id": "g2", "title": "G2", "objective": "O2"},
        ],
    }
    dict_save(tmp_path, "no-gp", data)

    chain_file = ChainStore.chains_dir() / "no-gp" / "chain.json"
    raw = json.loads(chain_file.read_text())
    assert "goal_planet" not in raw


def test_load_chain_no_mission_goal_key(tmp_path: Path) -> None:
    """Module-level save_chain does not write mission_goal key."""
    from aura.drones.chain_store import save_chain as dict_save

    data = {
        "id": "no-mg",
        "name": "No MG",
        "description": "No mission_goal test",
        "goals": [
            {"id": "g1", "title": "G1", "objective": "O1"},
            {"id": "g2", "title": "G2", "objective": "O2"},
        ],
    }
    dict_save(tmp_path, "no-mg", data)

    chain_file = ChainStore.chains_dir() / "no-mg" / "chain.json"
    raw = json.loads(chain_file.read_text())
    assert "mission_goal" not in raw


# ── _normalize_chain_data — goal normalization ─────────────────────


def test_normalize_chain_data_goal_id_to_id() -> None:
    """Legacy goal_id/goal keys are migrated to id/objective."""
    raw = {
        "id": "test",
        "name": "Test",
        "description": "",
        "goals": [
            {"goal_id": "g1", "goal": "First goal", "title": "G1"},
            {"goal_id": "g2", "goal": "Second goal", "title": "G2"},
        ],
    }
    result = _normalize_chain_data(raw)
    assert "goals" in result
    for g in result["goals"]:
        assert "id" in g
        assert "objective" in g
        assert "goal_id" not in g
        assert "goal" not in g
    assert result["goals"][0]["id"] == "g1"
    assert result["goals"][0]["objective"] == "First goal"
    assert result["goals"][1]["id"] == "g2"
    assert result["goals"][1]["objective"] == "Second goal"


def test_normalize_chain_data_canonical_wins() -> None:
    """Canonical id/objective values are preserved when both legacy and canonical exist."""
    raw = {
        "id": "test",
        "name": "Test",
        "description": "",
        "goals": [
            {
                "id": "canonical-id",
                "objective": "Canonical objective",
                "goal_id": "legacy-id",
                "goal": "Legacy objective",
                "title": "G1",
            },
        ],
    }
    result = _normalize_chain_data(raw)
    g = result["goals"][0]
    assert g["id"] == "canonical-id"
    assert g["objective"] == "Canonical objective"
    assert "goal_id" not in g
    assert "goal" not in g


def test_normalize_chain_data_strips_deprecated_keys() -> None:
    """mission_goal and goal_planet keys are removed from the result."""
    raw = {
        "id": "test",
        "name": "Test",
        "description": "",
        "mission_goal": "Do the thing",
        "goal_planet": {"goal": "Old planet"},
    }
    result = _normalize_chain_data(raw)
    assert "mission_goal" not in result
    assert "goal_planet" not in result


def test_normalize_chain_data_mission_goal_migrates_to_goals() -> None:
    """mission_goal migrates into goals list when goals is absent."""
    raw = {
        "id": "test",
        "name": "Test",
        "description": "",
        "mission_goal": "Mission objective",
    }
    result = _normalize_chain_data(raw)
    assert "goals" in result
    assert len(result["goals"]) == 1
    assert result["goals"][0]["objective"] == "Mission objective"
    assert "mission_goal" not in result


# ── list_chains normalization ──────────────────────────────────────


def test_module_list_chains_normalizes_goals(tmp_path: Path) -> None:
    """Module-level list_chains returns canonical goals (not legacy keys)."""
    from aura.drones.chain_store import save_chain as dict_save, list_chains as raw_list

    data = {
        "id": "legacy-chain",
        "name": "Legacy",
        "description": "",
        "goals": [
            {"goal_id": "g1", "goal": "Old style", "title": "G1"},
        ],
    }
    dict_save(tmp_path, "legacy-chain", data)
    chains = raw_list(tmp_path)
    found = [c for c in chains if c["id"] == "legacy-chain"]
    assert len(found) == 1
    for g in found[0].get("goals", []):
        assert "id" in g
        assert "objective" in g
        assert "goal_id" not in g
        assert "goal" not in g


def test_chainstore_list_chains_normalizes_goals(tmp_path: Path) -> None:
    """ChainStore.list_chains returns canonical goals (not legacy keys)."""
    from aura.drones.chain_store import save_chain as dict_save

    data = {
        "id": "legacy-chain",
        "name": "Legacy",
        "description": "",
        "goals": [
            {"goal_id": "g1", "goal": "Old style", "title": "G1"},
        ],
    }
    dict_save(tmp_path, "legacy-chain", data)
    store_chains = ChainStore.list_chains(tmp_path)
    found = [c for c in store_chains if c.id == "legacy-chain"]
    assert len(found) == 1
    for g in found[0].goals:
        assert g.id == "g1"
        assert g.objective == "Old style"


# ── validate — multi-goal goal_id checks ───────────────────────────


def test_validate_multigoal_blank_goal_id() -> None:
    """Assignment node with blank goal_id fails in multi-goal chain."""
    drone = _make_drone("drone-a")
    lookup = {"drone-a": drone}
    chain = ChainDefinition(
        id="multi",
        name="Multi",
        description="",
        goals=(
            ChainGoal(id="g1", title="G1", objective="O1"),
            ChainGoal(id="g2", title="G2", objective="O2"),
        ),
        nodes=(ChainNode(
            id="n1", drone_id="drone-a",
            is_assignment=True, goal_id="",
        ),),
    )
    result = validate(chain, lookup)
    assert result.ok is False
    assert any("no goal_id" in e.lower() for e in result.errors)


def test_validate_multigoal_unknown_goal_id() -> None:
    """Assignment node with unknown goal_id fails in multi-goal chain."""
    drone = _make_drone("drone-a")
    lookup = {"drone-a": drone}
    chain = ChainDefinition(
        id="multi",
        name="Multi",
        description="",
        goals=(
            ChainGoal(id="g1", title="G1", objective="O1"),
            ChainGoal(id="g2", title="G2", objective="O2"),
        ),
        nodes=(ChainNode(
            id="n1", drone_id="drone-a",
            is_assignment=True, goal_id="nonexistent",
        ),),
    )
    result = validate(chain, lookup)
    assert result.ok is False
    assert any("unknown goal_id" in e.lower() for e in result.errors)
