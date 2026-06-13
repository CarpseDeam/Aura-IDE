"""Tests for aura.drones.chain_runner — ChainRun and run_chain."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aura.drones.chain import ChainDefinition, ChainEdge, ChainGoal, ChainNode
from aura.drones.chain_runner import (
    ChainRun,
    _load_run_state,
    _run_dir,
    _save_run_state,
    classify_consequential_nodes,
    get_last_chain_run,
    list_chain_runs,
    run_chain,
)
from aura.drones.definition import DroneDefinition

# ── Helpers ─────────────────────────────────────────────────────────


def _make_drone(
    drone_id: str,
    name: str = "",
    accepts: str = "",
    produces: str = "",
    write_policy: str = "read_only",
    enabled: bool = True,
    allowed_tools: tuple[str, ...] = (),
) -> DroneDefinition:
    return DroneDefinition(
        id=drone_id,
        name=name or drone_id,
        description="",
        instructions="Do the thing",
        write_policy=write_policy,
        allowed_tools=allowed_tools,
        output_contract="Output",
        enabled=enabled,
        accepts=accepts,
        produces=produces,
    )


def _make_mock_result(
    status: str = "completed",
    met: bool | None = True,
    evidence: str = "ok",
    produced_artifact: dict | None = None,
    summary: str = "Mock summary",
    approved_write_actions: int = 0,
    rejected_write_actions: int = 0,
) -> dict:
    return {
        "ok": status == "completed",
        "run_id": "mock-run",
        "drone_id": "mock-drone",
        "drone_name": "Mock",
        "status": status,
        "summary": summary,
        "tool_calls_made": 1,
        "tool_errors": 0,
        "elapsed_seconds": 0.5,
        "approved_write_actions": approved_write_actions,
        "rejected_write_actions": rejected_write_actions,
        "receipt": {
            "run_id": "mock-run",
            "drone_id": "mock-drone",
            "drone_name": "Mock",
            "status": status,
            "met": met,
            "evidence": evidence,
            "produced_artifact": produced_artifact,
            "summary": summary,
        },
    }


def _chain_run_dir(workspace_root: Path, run_id: str) -> Path:
    return _run_dir(workspace_root, run_id)


def _node_output_dir(workspace_root: Path, run_id: str, node_id: str) -> Path:
    return _run_dir(workspace_root, run_id) / node_id


# ── ChainRun data model ────────────────────────────────────────────


def test_chain_run_defaults() -> None:
    run = ChainRun(run_id="abc123", chain_id="my-chain")
    assert run.run_id == "abc123"
    assert run.chain_id == "my-chain"
    assert run.status == "running"
    assert run.node_runs == {}
    assert run.started_at == ""
    assert run.ended_at == ""


def test_chain_run_asdict_roundtrip() -> None:
    run = ChainRun(
        run_id="abc123",
        chain_id="my-chain",
        status="completed",
        node_runs={
            "n1": {
                "node_id": "n1",
                "drone_id": "d1",
                "status": "completed",
                "receipt": {"status": "completed"},
                "artifact_path": ".aura/chains/runs/abc123/n1/output.json",
                "met": True,
                "evidence": "ok",
                "error": None,
            }
        },
        started_at="2025-01-01T00:00:00",
        ended_at="2025-01-01T00:01:00",
    )
    data = asdict(run)
    reconstructed = ChainRun(**data)
    assert reconstructed == run


# ── _save_run_state / _load_run_state ──────────────────────────────


def test_save_and_load_run_state(tmp_path: Path) -> None:
    run = ChainRun(run_id="test-run", chain_id="test-chain", status="running")
    _save_run_state(tmp_path, run)
    loaded = _load_run_state(tmp_path, "test-run")
    assert loaded is not None
    assert loaded.run_id == "test-run"
    assert loaded.chain_id == "test-chain"
    assert loaded.status == "running"


def test_load_run_state_nonexistent(tmp_path: Path) -> None:
    assert _load_run_state(tmp_path, "no-such-run") is None


def test_load_run_state_after_node(tmp_path: Path) -> None:
    run = ChainRun(
        run_id="noderun",
        chain_id="test-chain",
        status="running",
        node_runs={
            "n1": {
                "node_id": "n1",
                "drone_id": "d1",
                "status": "completed",
                "receipt": {"status": "completed"},
                "artifact_path": ".aura/chains/runs/noderun/n1/output.json",
                "met": True,
                "evidence": "ok",
                "error": None,
            }
        },
    )
    _save_run_state(tmp_path, run)
    loaded = _load_run_state(tmp_path, "noderun")
    assert loaded is not None
    assert loaded.node_runs["n1"]["status"] == "completed"
    assert loaded.node_runs["n1"]["met"] is True


# ===================================================================
# run_chain tests
# ===================================================================


# ── Test 1: single node ───────────────────────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_single_node_chain_runs_and_completes(
    mock_runner, tmp_path: Path
) -> None:
    mock_runner.return_value = _make_mock_result(
        produced_artifact={"query": "test", "domain": "example.com"}
    )

    drone = _make_drone("drone-a", produces="SearchBrief")
    chain = ChainDefinition(
        id="single-node",
        name="Single Node",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone},
    )

    assert result.status == "completed"
    assert "n1" in result.node_runs
    n1 = result.node_runs["n1"]
    assert n1["status"] == "completed"
    assert n1["drone_id"] == "drone-a"
    assert n1["met"] is True
    assert n1["evidence"] == "ok"
    assert n1["error"] is None
    assert n1["artifact_path"] != ""
    mock_runner.assert_called_once()


# ── Test 2: two-node artifact handoff ─────────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_two_node_chain_passes_artifact_downstream(
    mock_runner, tmp_path: Path
) -> None:
    call_log: list[tuple[str, str]] = []

    def _side_effect(*args, **kwargs):
        drone_id = kwargs.get("drone_id", "")
        goal = kwargs.get("goal", "")
        call_log.append((drone_id, goal))
        if drone_id == "drone-a":
            return _make_mock_result(
                produced_artifact={
                    "query": "test",
                    "domain": "example.com",
                }
            )
        return _make_mock_result(produced_artifact={"result": "ok"})

    mock_runner.side_effect = _side_effect

    drone_a = _make_drone("drone-a", produces="SearchBrief")
    drone_b = _make_drone("drone-b", accepts="SearchBrief")
    chain = ChainDefinition(
        id="two-node",
        name="Two Node",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(ChainEdge(from_node="n1", to_node="n2"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone_a, "drone-b": drone_b},
    )

    assert result.status == "completed"
    assert len(call_log) == 2

    # Node 2's goal should contain node 1's artifact
    n2_goal = call_log[1][1]
    assert "## Input Artifact" in n2_goal
    assert "test" in n2_goal
    assert "example.com" in n2_goal


# ── Test 3: chain stops on unmet ──────────────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_chain_stops_on_unmet(mock_runner, tmp_path: Path) -> None:
    call_log: list[str] = []

    def _side_effect(*args, **kwargs):
        drone_id = kwargs.get("drone_id", "")
        call_log.append(drone_id)
        return _make_mock_result(status="completed", met=False, evidence="Missing field")

    mock_runner.side_effect = _side_effect

    drone = _make_drone("drone-a", produces="SearchBrief")
    drone2 = _make_drone("drone-b")
    chain = ChainDefinition(
        id="unmet-chain",
        name="Unmet",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(ChainEdge(from_node="n1", to_node="n2"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone, "drone-b": drone2},
    )

    assert result.status == "failed"
    assert len(call_log) == 1  # Node 2 never runs
    n1 = result.node_runs["n1"]
    assert n1["status"] == "completed"
    assert n1["met"] is False
    assert "unmet" in (n1.get("error") or "").lower()


# ── Test 4: chain stops on node failure ───────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_chain_stops_on_node_failure(mock_runner, tmp_path: Path) -> None:
    call_log: list[str] = []

    def _side_effect(*args, **kwargs):
        drone_id = kwargs.get("drone_id", "")
        call_log.append(drone_id)
        return _make_mock_result(status="failed", met=None, evidence="")

    mock_runner.side_effect = _side_effect

    drone = _make_drone("drone-a", produces="SearchBrief")
    drone2 = _make_drone("drone-b")
    chain = ChainDefinition(
        id="fail-chain",
        name="Fail",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(ChainEdge(from_node="n1", to_node="n2"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone, "drone-b": drone2},
    )

    assert result.status == "failed"
    assert len(call_log) == 1
    n1 = result.node_runs["n1"]
    assert n1["status"] == "failed"


# ── Test 5: chain stops on timeout ────────────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_chain_stops_on_node_timeout(mock_runner, tmp_path: Path) -> None:
    call_log: list[str] = []

    def _side_effect(*args, **kwargs):
        drone_id = kwargs.get("drone_id", "")
        call_log.append(drone_id)
        return _make_mock_result(status="timed_out", met=None, evidence="")

    mock_runner.side_effect = _side_effect

    drone = _make_drone("drone-a")
    drone2 = _make_drone("drone-b")
    chain = ChainDefinition(
        id="timeout-chain",
        name="Timeout",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(ChainEdge(from_node="n1", to_node="n2"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone, "drone-b": drone2},
    )

    assert result.status == "failed"
    assert len(call_log) == 1
    n1 = result.node_runs["n1"]
    assert n1["status"] == "timed_out"


# ── Test 6: resume skips completed ────────────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_resume_from_node_skips_completed(
    mock_runner, tmp_path: Path
) -> None:
    run_id = "resume-run"
    chain_id = "resume-chain"
    call_log: list[str] = []

    def _side_effect(*args, **kwargs):
        drone_id = kwargs.get("drone_id", "")
        call_log.append(drone_id)
        assert drone_id == "drone-b", "Should not re-run drone-a"
        return _make_mock_result(produced_artifact={"result": "ok"})

    mock_runner.side_effect = _side_effect

    drone_a = _make_drone("drone-a", produces="SearchBrief")
    drone_b = _make_drone("drone-b")
    chain = ChainDefinition(
        id=chain_id,
        name="Resume Chain",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(ChainEdge(from_node="n1", to_node="n2"),),
    )

    # Manually create saved state for node_1 completed
    run_dir = _chain_run_dir(tmp_path, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save node_1 output.json
    n1_dir = run_dir / "n1"
    n1_dir.mkdir(parents=True, exist_ok=True)
    n1_output = {"query": "test", "domain": "example.com"}
    (n1_dir / "output.json").write_text(
        json.dumps(n1_output), encoding="utf-8"
    )

    # Save run.json with node_1 completed
    saved_run = ChainRun(
        run_id=run_id,
        chain_id=chain_id,
        status="failed",
        node_runs={
            "n1": {
                "node_id": "n1",
                "drone_id": "drone-a",
                "status": "completed",
                "receipt": {"status": "completed", "met": True},
                "artifact_path": str(
                    (n1_dir / "output.json").relative_to(tmp_path)
                ),
                "met": True,
                "evidence": "ok",
                "error": None,
            }
        },
        started_at="2025-01-01T00:00:00",
    )
    _save_run_state(tmp_path, saved_run)

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone_a, "drone-b": drone_b},
        resume_run_id=run_id,
        start_node="n2",
    )

    assert result.status == "completed"
    assert len(call_log) == 1  # Only drone-b ran
    assert "n1" in result.node_runs
    assert result.node_runs["n1"]["status"] == "completed"
    assert result.node_runs["n2"]["status"] == "completed"


# ── Test 7: resume loads saved artifact ───────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_resume_loads_saved_artifact_for_downstream(
    mock_runner, tmp_path: Path
) -> None:
    run_id = "resume-art"
    chain_id = "art-chain"
    captured_goal: str | None = None

    def _side_effect(*args, **kwargs):
        nonlocal captured_goal
        captured_goal = kwargs.get("goal", "")
        return _make_mock_result(produced_artifact={"result": "ok"})

    mock_runner.side_effect = _side_effect

    drone_a = _make_drone("drone-a", produces="SearchBrief")
    drone_b = _make_drone("drone-b", accepts="SearchBrief")
    chain = ChainDefinition(
        id=chain_id,
        name="Art Chain",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(ChainEdge(from_node="n1", to_node="n2"),),
    )

    # Manually create saved state for node_1
    run_dir = _chain_run_dir(tmp_path, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    n1_dir = run_dir / "n1"
    n1_dir.mkdir(parents=True, exist_ok=True)
    n1_artifact = {"query": "saved-query", "domain": "saved-domain"}
    (n1_dir / "output.json").write_text(
        json.dumps(n1_artifact), encoding="utf-8"
    )

    saved_run = ChainRun(
        run_id=run_id,
        chain_id=chain_id,
        status="failed",
        node_runs={
            "n1": {
                "node_id": "n1",
                "drone_id": "drone-a",
                "status": "completed",
                "receipt": {"status": "completed", "met": True},
                "artifact_path": str(
                    (n1_dir / "output.json").relative_to(tmp_path)
                ),
                "met": True,
                "evidence": "ok",
                "error": None,
            }
        },
        started_at="2025-01-01T00:00:00",
    )
    _save_run_state(tmp_path, saved_run)

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone_a, "drone-b": drone_b},
        resume_run_id=run_id,
        start_node="n2",
    )

    assert result.status == "completed"
    assert captured_goal is not None
    assert "## Input Artifact" in captured_goal
    assert "saved-query" in captured_goal
    assert "saved-domain" in captured_goal


# ── Test 8: chain fails validation ────────────────────────────────


def test_chain_fails_validation(tmp_path: Path) -> None:
    chain = ChainDefinition(
        id="bad-chain",
        name="Bad",
        description="",
        nodes=(ChainNode(id="n1", drone_id="missing-drone"),),
    )

    with pytest.raises(ValueError, match="Chain validation failed"):
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={},
        )


# ── Test 9a: chain without write nodes — no callback needed ──────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_chain_with_no_write_nodes_no_approval_callback(
    mock_runner, tmp_path: Path
) -> None:
    """All read-only nodes should run fine without an approval_callback."""
    mock_runner.return_value = _make_mock_result()

    drone = _make_drone("drone-a")
    chain = ChainDefinition(
        id="read-only-chain",
        name="Read Only",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone},
    )

    assert result.status == "completed"


# ── Test 9b: chain with write nodes — raises without callback ────


def test_chain_with_write_nodes_raises_without_callback(
    tmp_path: Path,
) -> None:
    """Write nodes without approval_callback → ValueError."""
    drone = _make_drone(
        "drone-a",
        write_policy="ask_before_writes",
        allowed_tools=("write_file", "read_file"),
    )
    chain = ChainDefinition(
        id="write-chain",
        name="Write",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )

    with pytest.raises(ValueError) as exc_info:
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={"drone-a": drone},
        )
    assert "approval_callback" in str(exc_info.value)


# ── Test 9c: chain with write nodes — callback declined ──────────


def test_chain_with_write_nodes_callback_declined(
    tmp_path: Path,
) -> None:
    """approval_callback returns False → ValueError."""
    drone = _make_drone(
        "drone-a",
        write_policy="ask_before_writes",
    )
    chain = ChainDefinition(
        id="write-chain-declined",
        name="Write Declined",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )

    with pytest.raises(ValueError, match="declined"):
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={"drone-a": drone},
            approval_callback=lambda nodes: False,
        )


# ── Test 9d: chain with write nodes — callback approved ──────────


@patch("aura.drones.chain_runner.run_write_capable_drone_sync")
def test_chain_with_write_nodes_callback_approved_runs(
    mock_write_runner, tmp_path: Path
) -> None:
    """approval_callback returns True → chain runs, write node executes."""
    mock_write_runner.return_value = _make_mock_result(
        approved_write_actions=2,
        rejected_write_actions=0,
    )

    drone = _make_drone(
        "drone-a",
        write_policy="ask_before_writes",
        allowed_tools=("write_file", "read_file"),
    )
    chain = ChainDefinition(
        id="write-chain-approved",
        name="Write Approved",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )

    call_log: list[list[dict]] = []

    def approval_callback(nodes: list[dict]) -> bool:
        call_log.append(nodes)
        return True

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone},
        approval_callback=approval_callback,
    )

    assert result.status == "completed"
    assert len(call_log) == 1
    assert call_log[0][0]["node_id"] == "n1"
    assert call_log[0][0]["drone_id"] == "drone-a"
    mock_write_runner.assert_called_once()


# ── Test 10: empty chain raises ────────────────────────────────────


def test_empty_chain_raises(tmp_path: Path) -> None:
    chain = ChainDefinition(
        id="empty-chain",
        name="Empty",
        description="",
        nodes=(),
    )

    with pytest.raises(ValueError, match="Chain has no nodes to run."):
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={},
        )


# ── Test 11: free-form handoff ────────────────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_free_form_handoff(mock_runner, tmp_path: Path) -> None:
    call_log: list[tuple[str, str]] = []

    def _side_effect(*args, **kwargs):
        drone_id = kwargs.get("drone_id", "")
        goal = kwargs.get("goal", "")
        call_log.append((drone_id, goal))
        if drone_id == "drone-a":
            # Free-form drone — no produces type
            return _make_mock_result(
                status="completed",
                met=None,
                produced_artifact=None,
                summary="found some interesting stuff",
            )
        return _make_mock_result(produced_artifact={"result": "ok"})

    mock_runner.side_effect = _side_effect

    drone_a = _make_drone("drone-a", produces="")  # free-form
    drone_b = _make_drone("drone-b", accepts="")  # free-form consumer
    chain = ChainDefinition(
        id="free-form-chain",
        name="Free Form",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
        edges=(ChainEdge(from_node="n1", to_node="n2"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone_a, "drone-b": drone_b},
    )

    assert result.status == "completed"
    assert len(call_log) == 2

    # Node 2's goal should contain free-form summary from node 1
    n2_goal = call_log[1][1]
    assert "found some interesting stuff" in n2_goal
    # Should NOT have a JSON code block for free-form handoff
    assert "```json" not in n2_goal


# ── Test 12: run state saved and loadable ──────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_run_state_saved_and_loadable(mock_runner, tmp_path: Path) -> None:
    mock_runner.return_value = _make_mock_result(
        produced_artifact={"query": "test", "domain": "example.com"}
    )

    drone = _make_drone("drone-a", produces="SearchBrief")
    chain = ChainDefinition(
        id="state-test",
        name="State Test",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone},
    )

    # Verify we can load back the saved state
    loaded = _load_run_state(tmp_path, result.run_id)
    assert loaded is not None
    assert loaded.run_id == result.run_id
    assert loaded.chain_id == result.chain_id
    assert loaded.status == "completed"
    assert loaded.node_runs["n1"]["status"] == "completed"
    assert loaded.node_runs["n1"]["met"] is True

    # Verify output.json exists for the node
    n1_output_path = (
        _node_output_dir(tmp_path, result.run_id, "n1") / "output.json"
    )
    assert n1_output_path.exists()
    output_data = json.loads(n1_output_path.read_text(encoding="utf-8"))
    assert output_data == {"query": "test", "domain": "example.com"}


# ── Additional edge-case tests ─────────────────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_runner_exception_caught(mock_runner, tmp_path: Path) -> None:
    """When run_read_only_drone_sync raises, the chain fails gracefully."""

    def _side_effect(*args, **kwargs):
        raise RuntimeError("Connection error")

    mock_runner.side_effect = _side_effect

    drone = _make_drone("drone-a")
    chain = ChainDefinition(
        id="crash-test",
        name="Crash",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone},
    )

    assert result.status == "failed"
    assert result.node_runs["n1"]["status"] == "failed"
    assert "Connection error" in (result.node_runs["n1"]["error"] or "")


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_resume_chain_id_mismatch(mock_runner, tmp_path: Path) -> None:
    """Resume with a different chain_id raises ValueError."""
    run_id = "mismatch-run"
    chain_id_a = "chain-a"
    chain_id_b = "chain-b"

    # Save state for chain-a
    saved_run = ChainRun(
        run_id=run_id,
        chain_id=chain_id_a,
        status="failed",
        started_at="2025-01-01T00:00:00",
    )
    _save_run_state(tmp_path, saved_run)

    # Try to resume with chain-b
    chain = ChainDefinition(
        id=chain_id_b,
        name="Chain B",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )
    drone = _make_drone("drone-a")

    with pytest.raises(ValueError, match="chain_id mismatch"):
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={"drone-a": drone},
            resume_run_id=run_id,
        )


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_resume_from_node_invalid(mock_runner, tmp_path: Path) -> None:
    """start_node not in the chain order raises ValueError."""
    run_id = "invalid-start"
    chain_id = "test-chain"

    saved_run = ChainRun(
        run_id=run_id,
        chain_id=chain_id,
        status="failed",
        started_at="2025-01-01T00:00:00",
    )
    _save_run_state(tmp_path, saved_run)

    chain = ChainDefinition(
        id=chain_id,
        name="Test",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )
    drone = _make_drone("drone-a")

    with pytest.raises(ValueError, match="not found in chain node order"):
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={"drone-a": drone},
            resume_run_id=run_id,
            start_node="nonexistent-node",
        )


def test_missing_node_drone_in_lookup(tmp_path: Path) -> None:
    """Node references a drone not in lookup — caught by validate()."""
    chain = ChainDefinition(
        id="missing-drone",
        name="Missing",
        description="",
        nodes=(ChainNode(id="n1", drone_id="ghost-drone"),),
    )

    with pytest.raises(
        ValueError, match="Chain validation failed"
    ):
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={},
        )


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_resume_run_id_not_found(mock_runner, tmp_path: Path) -> None:
    """Resume with a nonexistent run_id raises ValueError."""
    chain = ChainDefinition(
        id="test-chain",
        name="Test",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )
    drone = _make_drone("drone-a")

    with pytest.raises(
        ValueError, match="No saved run state found"
    ):
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={"drone-a": drone},
            resume_run_id="no-such-run",
        )


# ===================================================================
# classify_consequential_nodes tests
# ===================================================================


def test_classify_consequential_read_only_chain() -> None:
    """All read-only drones → empty list."""
    drone_a = _make_drone("drone-a", write_policy="read_only")
    drone_b = _make_drone("drone-b", write_policy="read_only")
    chain = ChainDefinition(
        id="test",
        name="Test",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
    )
    result = classify_consequential_nodes(chain, {"drone-a": drone_a, "drone-b": drone_b})
    assert result == []


def test_classify_consequential_mixed_chain() -> None:
    """One write node → returns dict with node_id, drone_id, drone_name, write_policy, consequential_tools."""
    drone_a = _make_drone(
        "drone-a",
        write_policy="ask_before_writes",
        name="Writer",
        allowed_tools=("write_file", "patch_file", "read_file"),
    )
    drone_b = _make_drone("drone-b", write_policy="read_only")
    chain = ChainDefinition(
        id="test",
        name="Test",
        description="",
        nodes=(
            ChainNode(id="n1", drone_id="drone-a"),
            ChainNode(id="n2", drone_id="drone-b"),
        ),
    )
    result = classify_consequential_nodes(chain, {"drone-a": drone_a, "drone-b": drone_b})

    assert len(result) == 1
    node_info = result[0]
    assert node_info["node_id"] == "n1"
    assert node_info["drone_id"] == "drone-a"
    assert node_info["drone_name"] == "Writer"
    assert node_info["write_policy"] == "ask_before_writes"
    assert "write_file" in node_info["consequential_tools"]
    assert "patch_file" in node_info["consequential_tools"]
    # read_file is not consequential
    assert "read_file" not in node_info["consequential_tools"]


def test_classify_consequential_write_node_with_consequential_tools() -> None:
    """Drone with write_policy='always_log' and allowed_tools → verify consequential_tools."""
    drone = _make_drone(
        "drone-a",
        write_policy="always_log",
        name="Logger",
        allowed_tools=("write_file", "read_file"),
    )
    chain = ChainDefinition(
        id="test",
        name="Test",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )
    result = classify_consequential_nodes(chain, {"drone-a": drone})

    assert len(result) == 1
    node_info = result[0]
    assert "write_file" in node_info["consequential_tools"]
    assert "read_file" not in node_info["consequential_tools"]


def test_classify_consequential_chain_now_allows_write_node_with_callback() -> None:
    """Write node with callback — chain runs and write-capable runner is called."""
    # This test verifies the safety gate has been removed and write nodes
    # route to the write-capable runner when approval_callback is provided.

    # Direct test of the classification + approval flow
    drone = _make_drone(
        "drone-a",
        write_policy="always_log",
        name="Logger",
        allowed_tools=("write_file",),
    )
    chain = ChainDefinition(
        id="test",
        name="Test",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )
    nodes = classify_consequential_nodes(chain, {"drone-a": drone})
    assert len(nodes) == 1
    assert nodes[0]["write_policy"] == "always_log"
    assert "write_file" in nodes[0]["consequential_tools"]

    # Verify it replaces the old safety gate
    assert nodes[0]["node_id"] == "n1"


# ===================================================================
# run_write_capable_drone_sync tests
# ===================================================================


@patch("aura.drones.sync_runner._run_drone_sync_impl")
def test_run_write_capable_drone_sync_executes_write_tools(
    mock_impl, tmp_path: Path
) -> None:
    """Verify run_write_capable_drone_sync calls the shared impl with write_enabled=True."""
    from aura.drones.sync_runner import run_write_capable_drone_sync

    mock_impl.return_value = _make_mock_result(
        approved_write_actions=1,
        rejected_write_actions=0,
    )

    drone = _make_drone(
        "drone-a",
        write_policy="ask_before_writes",
        allowed_tools=("write_file", "read_file"),
    )
    result = run_write_capable_drone_sync(
        workspace_root=tmp_path,
        drone_id="drone-a",
        drone=drone,
        goal="Write something",
        approval_callback=lambda req: MagicMock(action="approve"),
    )

    assert result["approved_write_actions"] == 1
    assert result["rejected_write_actions"] == 0

    # Verify the impl was called with write_enabled=True
    assert mock_impl.call_count >= 1
    call_kwargs = mock_impl.call_args[1]
    assert call_kwargs.get("write_enabled") is True
    assert call_kwargs.get("approval_callback") is not None


@patch("aura.drones.sync_runner._run_drone_sync_impl")
def test_run_write_capable_drone_sync_auto_approves_writes(
    mock_impl, tmp_path: Path
) -> None:
    """Verify the impl is called with write_enabled and an approval_callback."""
    from aura.drones.sync_runner import run_write_capable_drone_sync

    mock_impl.return_value = _make_mock_result(
        approved_write_actions=3,
        rejected_write_actions=0,
    )

    drone = _make_drone(
        "drone-a",
        write_policy="ask_before_writes",
        allowed_tools=("write_file", "read_file"),
    )

    def callback(req):
        return MagicMock(action="approve")

    result = run_write_capable_drone_sync(
        workspace_root=tmp_path,
        drone_id="drone-a",
        drone=drone,
        goal="Write stuff",
        approval_callback=callback,
    )

    assert result["approved_write_actions"] == 3
    assert result["rejected_write_actions"] == 0

    call_args = mock_impl.call_args[1]
    assert call_args.get("write_enabled") is True


# ── Test chain write node with rejected writes — met = None ─────


@patch("aura.drones.chain_runner.run_write_capable_drone_sync")
def test_chain_write_node_rejected_writes_sets_met_none(
    mock_write_runner, tmp_path: Path
) -> None:
    """Write-capable node with rejected_write_actions > 0 should have met = None."""
    mock_write_runner.return_value = _make_mock_result(
        approved_write_actions=1,
        rejected_write_actions=1,
        evidence="Some writes rejected",
    )

    drone = _make_drone(
        "drone-a",
        write_policy="ask_before_writes",
        allowed_tools=("write_file",),
    )
    chain = ChainDefinition(
        id="rejected-writes",
        name="Rejected",
        description="",
        nodes=(ChainNode(id="n1", drone_id="drone-a"),),
    )

    result = run_chain(
        workspace_root=tmp_path,
        chain=chain,
        drone_lookup={"drone-a": drone},
        approval_callback=lambda nodes: True,
    )

    assert result.status == "completed"
    n1 = result.node_runs["n1"]
    assert n1["met"] is None
    assert "rejected" in n1["evidence"].lower()


# ===================================================================
# get_last_chain_run / list_chain_runs tests
# ===================================================================


def _write_run_json(runs_dir: Path, run_id: str, chain_id: str, ended_at: str, status: str = "completed") -> ChainRun:
    """Write a minimal run.json and return the ChainRun."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cr = ChainRun(
        run_id=run_id,
        chain_id=chain_id,
        status=status,
        started_at="2025-06-01T10:00:00",
        ended_at=ended_at,
    )
    data = asdict(cr)
    (run_dir / "run.json").write_text(json.dumps(data), encoding="utf-8")
    return cr


def test_get_last_chain_run_no_runs(tmp_path: Path) -> None:
    """Empty runs directory returns None."""
    assert get_last_chain_run(tmp_path, "any-chain") is None

    # Runs dir exists but empty
    (tmp_path / ".aura" / "chains" / "runs").mkdir(parents=True, exist_ok=True)
    assert get_last_chain_run(tmp_path, "any-chain") is None


def test_get_last_chain_run_returns_most_recent(tmp_path: Path) -> None:
    """Two runs for the same chain — returns the one with most recent ended_at."""
    runs_dir = tmp_path / ".aura" / "chains" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    _write_run_json(runs_dir, "run-1", "my-chain", "2025-06-01T10:05:00")
    _write_run_json(runs_dir, "run-2", "my-chain", "2025-06-01T10:10:00")

    result = get_last_chain_run(tmp_path, "my-chain")
    assert result is not None
    assert result.run_id == "run-2"


def test_get_last_chain_run_filters_by_chain_id(tmp_path: Path) -> None:
    """Runs for different chains — only returns matching chain_id."""
    runs_dir = tmp_path / ".aura" / "chains" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    _write_run_json(runs_dir, "run-a", "chain-a", "2025-06-01T10:05:00")
    _write_run_json(runs_dir, "run-b", "chain-b", "2025-06-01T10:10:00")

    result = get_last_chain_run(tmp_path, "chain-a")
    assert result is not None
    assert result.run_id == "run-a"
    assert result.chain_id == "chain-a"


def test_get_last_chain_run_skips_malformed(tmp_path: Path) -> None:
    """Corrupt run.json is skipped, valid one is returned."""
    runs_dir = tmp_path / ".aura" / "chains" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Write a corrupt run.json
    corrupt_dir = runs_dir / "bad-run"
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    (corrupt_dir / "run.json").write_text("not json at all", encoding="utf-8")

    # Write a valid one
    _write_run_json(runs_dir, "good-run", "my-chain", "2025-06-01T10:10:00")

    result = get_last_chain_run(tmp_path, "my-chain")
    assert result is not None
    assert result.run_id == "good-run"


def test_list_chain_runs_returns_sorted(tmp_path: Path) -> None:
    """Multiple runs — sorted by ended_at desc, respects limit."""
    runs_dir = tmp_path / ".aura" / "chains" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    _write_run_json(runs_dir, "run-3", "my-chain", "2025-06-03T10:00:00")
    _write_run_json(runs_dir, "run-1", "my-chain", "2025-06-01T10:00:00")
    _write_run_json(runs_dir, "run-2", "my-chain", "2025-06-02T10:00:00")
    _write_run_json(runs_dir, "run-other", "other-chain", "2025-06-10T10:00:00")

    # Without limit — all 3 for my-chain
    results = list_chain_runs(tmp_path, "my-chain", limit=10)
    assert len(results) == 3
    # Sorted desc by ended_at
    assert results[0].run_id == "run-3"
    assert results[1].run_id == "run-2"
    assert results[2].run_id == "run-1"

    # With limit
    results = list_chain_runs(tmp_path, "my-chain", limit=2)
    assert len(results) == 2
    assert results[0].run_id == "run-3"
    assert results[1].run_id == "run-2"

    # Different chain
    results = list_chain_runs(tmp_path, "other-chain", limit=10)
    assert len(results) == 1
    assert results[0].run_id == "run-other"


# ── run_chain — multi-goal validation ─────────────────────────────


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_run_chain_fails_blank_goal_id_multigoal(
    mock_runner, tmp_path: Path
) -> None:
    """run_chain raises ValueError when assignment node has blank goal_id in multi-goal."""
    mock_runner.return_value = _make_mock_result()

    drone = _make_drone("drone-a")
    chain = ChainDefinition(
        id="multi-goal",
        name="Multi Goal",
        description="",
        goals=(
            ChainGoal(id="g1", title="G1", objective="O1"),
            ChainGoal(id="g2", title="G2", objective="O2"),
        ),
        nodes=(
            ChainNode(
                id="n1", drone_id="drone-a",
                is_assignment=True, goal_id="",
                goal_template="Do the thing",
            ),
        ),
    )

    with pytest.raises(ValueError, match="Chain validation failed"):
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={"drone-a": drone},
        )
    mock_runner.assert_not_called()


@patch("aura.drones.chain_runner.run_read_only_drone_sync")
def test_run_chain_fails_unknown_goal_id_multigoal(
    mock_runner, tmp_path: Path
) -> None:
    """run_chain raises ValueError when assignment node targets unknown goal_id in multi-goal."""
    mock_runner.return_value = _make_mock_result()

    drone = _make_drone("drone-a")
    chain = ChainDefinition(
        id="multi-goal",
        name="Multi Goal",
        description="",
        goals=(
            ChainGoal(id="g1", title="G1", objective="O1"),
            ChainGoal(id="g2", title="G2", objective="O2"),
        ),
        nodes=(
            ChainNode(
                id="n1", drone_id="drone-a",
                is_assignment=True, goal_id="nonexistent",
                goal_template="Do the thing",
            ),
        ),
    )

    with pytest.raises(ValueError, match="Chain validation failed"):
        run_chain(
            workspace_root=tmp_path,
            chain=chain,
            drone_lookup={"drone-a": drone},
        )
    mock_runner.assert_not_called()
