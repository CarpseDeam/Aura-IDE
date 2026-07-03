"""Tests for the research adapter and folder-runner web-research seam.

These now verify that web-research no longer carries silent/headless
browser flags — the ResearchBrowserController is the sole browser owner.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from aura.conversation.tools.registry import ToolRegistry
from aura.drones.definition import DroneBudget, DroneDefinition
from aura.drones.background_runner import ReadOnlyDroneBackgroundRunner
from aura.drones.folder_runner import _run_command_drone, run_folder_drone_sync
from aura.research.adapter import WEB_RESEARCH_DRONE_ID, build_adapter_call
from aura.research.request import build_research_request


def test_answer_only_adapter_call_does_not_set_headless():
    """The adapter still builds a call, but browser flags are inert defaults.

    The controller, not the upstream contract, owns browser decisions.
    """
    request = build_research_request("Are there any World Cup matches today?")

    call = build_adapter_call(request)

    assert call.drone_id == WEB_RESEARCH_DRONE_ID
    assert call.goal == "Are there any World Cup matches today?"
    # The upstream contains inert defaults — not silent/headless
    assert call.upstream["research_ui"]["ui_mode"] == "visible"
    assert call.upstream["research_ui"]["headless"] is False
    assert call.upstream["research_ui"]["visible"] is True
    # Top-level flags are also inert
    assert call.upstream.get("headless") is False


def test_web_research_folder_runner_no_longer_passes_silent_env(
    tmp_path,
    monkeypatch,
):
    """Folder runner no longer sets silent research env vars."""
    folder = tmp_path / WEB_RESEARCH_DRONE_ID
    folder.mkdir()
    probe = folder / "probe.py"
    probe.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import sys",
                "payload = json.loads(sys.stdin.read())",
                "print(json.dumps({",
                "    'ok': True,",
                "    'summary': 'probe complete',",
                "    'cargo': {",
                "        'payload': payload,",
                "        'env': {",
                "            'AURA_RESEARCH_UI_MODE': os.environ.get('AURA_RESEARCH_UI_MODE'),",
                "            'AURA_WEB_RESEARCH_HEADLESS': os.environ.get('AURA_WEB_RESEARCH_HEADLESS'),",
                "            'AURA_WEB_RESEARCH_VISIBLE': os.environ.get('AURA_WEB_RESEARCH_VISIBLE'),",
                "        },",
                "    },",
                "}))",
            ]
        ),
        encoding="utf-8",
    )
    manifest = {
        "name": "Web Research",
        "description": "Probe",
        "instructions": "Probe",
        "kind": "command",
        "write_policy": "read_only",
        "entrypoint": {
            "kind": "command",
            "command": [sys.executable, "probe.py"],
            "protocol": "json-stdio",
        },
        "budget": {"timeout_seconds": 30},
        "output_contract": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "summary": {"type": "string"},
            },
            "required": ["ok", "summary"],
        },
    }
    (folder / "drone.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        "aura.drones.folder_runner.DroneStore.drone_folder",
        lambda workspace_root, drone_id: folder,
    )
    drone = DroneDefinition(
        id=WEB_RESEARCH_DRONE_ID,
        name="Web Research",
        description="Probe",
        instructions="Probe",
        write_policy="read_only",
        output_contract=manifest["output_contract"],
        budget=DroneBudget(timeout_seconds=30),
        entrypoint=manifest["entrypoint"],
    )
    call = build_adapter_call(
        build_research_request("Are there any World Cup matches today?")
    )

    result = run_folder_drone_sync(
        workspace_root=tmp_path,
        drone_id=WEB_RESEARCH_DRONE_ID,
        drone=drone,
        goal=call.goal,
        upstream=call.upstream,
    )

    payload = result["cargo"]["payload"]
    env = result["cargo"]["env"]
    # Upstream still flows through, but with inert (visible) defaults
    assert payload["upstream"]["research_ui"]["ui_mode"] == "visible"
    assert payload["upstream"]["research_ui"]["headless"] is False
    # No env vars are set by the controller-ownership path
    assert env["AURA_RESEARCH_UI_MODE"] is None
    assert env["AURA_WEB_RESEARCH_HEADLESS"] is None
    assert env["AURA_WEB_RESEARCH_VISIBLE"] is None


def test_web_research_now_runs_via_subprocess(
    tmp_path,
    monkeypatch,
):
    """Web research now always takes the subprocess path (no in-process special case)."""
    folder = tmp_path / WEB_RESEARCH_DRONE_ID
    folder.mkdir()
    (folder / "drone.json").write_text(json.dumps({
        "name": "Web Research",
        "description": "Probe",
        "instructions": "Probe",
        "kind": "command",
        "write_policy": "read_only",
        "entrypoint": {
            "kind": "command",
            "command": ["python", "probe.py"],
            "protocol": "json-stdio",
        },
        "budget": {"timeout_seconds": 30},
        "output_contract": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "summary": {"type": "string"}},
            "required": ["ok", "summary"],
        },
    }), encoding="utf-8")

    monkeypatch.setattr(
        "aura.drones.folder_runner.DroneStore.drone_folder",
        lambda workspace_root, drone_id: folder,
    )
    captured = {}

    def fake_command_drone(folder_arg, entrypoint, payload, **kw):
        captured["folder"] = folder_arg
        captured["payload"] = payload
        return {"ok": True, "summary": "done"}

    monkeypatch.setattr(
        "aura.drones.folder_runner._run_command_drone",
        fake_command_drone,
    )

    drone = DroneDefinition(
        id=WEB_RESEARCH_DRONE_ID,
        name="Web Research",
        description="Probe",
        instructions="Probe",
        write_policy="read_only",
        output_contract={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "summary": {"type": "string"}},
            "required": ["ok", "summary"],
        },
        budget=DroneBudget(timeout_seconds=30),
        entrypoint={
            "kind": "command",
            "command": [sys.executable, "probe.py"],
            "protocol": "json-stdio",
        },
    )
    call = build_adapter_call(
        build_research_request("Are there any World Cup matches today?")
    )

    result = run_folder_drone_sync(
        workspace_root=tmp_path,
        drone_id=WEB_RESEARCH_DRONE_ID,
        drone=drone,
        goal=call.goal,
        upstream=call.upstream,
    )

    assert result["ok"] is True
    # Verifies _run_command_drone was called (subprocess path), not in-process
    assert captured["folder"] == folder


def test_folder_drone_subprocess_receives_no_window_kwargs(tmp_path, monkeypatch):
    captured = {}
    startupinfo = object()

    class FakeProcess:
        returncode = 0
        args = ["python", "probe.py"]

        def communicate(self, input=None, timeout=None):
            return json.dumps({"ok": True, "summary": "done"}), ""

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        "aura.drones.folder_runner.get_subprocess_kwargs",
        lambda: {"creationflags": 12345, "startupinfo": startupinfo},
    )
    monkeypatch.setattr("aura.drones.folder_runner.subprocess.Popen", fake_popen)

    result = _run_command_drone(
        tmp_path,
        {"command": [sys.executable, "probe.py"]},
        {"goal": "test"},
        timeout_seconds=30,
    )

    assert result["ok"] is True
    assert captured["kwargs"]["creationflags"] == 12345
    assert captured["kwargs"]["startupinfo"] is startupinfo


def test_background_runner_preserves_web_research_upstream(
    tmp_path,
    monkeypatch,
):
    captured = {}

    def fake_run_read_only_drone_sync(**kwargs):
        captured.update(kwargs)
        return {
            "status": "completed",
            "summary": "done",
            "tool_calls_made": 0,
            "tool_errors": 0,
            "elapsed_seconds": 0.0,
            "receipt": {},
        }

    monkeypatch.setattr(
        "aura.drones.background_runner.run_read_only_drone_sync",
        fake_run_read_only_drone_sync,
    )
    runner = ReadOnlyDroneBackgroundRunner(tmp_path, max_parallel=1)
    drone = DroneDefinition(
        id=WEB_RESEARCH_DRONE_ID,
        name="Web Research",
        description="Probe",
        instructions="Probe",
        write_policy="read_only",
        output_contract={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "summary": {"type": "string"}},
            "required": ["ok", "summary"],
        },
        budget=DroneBudget(timeout_seconds=30),
        entrypoint={"kind": "command", "command": [sys.executable], "protocol": "json-stdio"},
    )
    call = build_adapter_call(
        build_research_request("Are there any World Cup matches today?")
    )

    job = runner.launch(drone, call.goal, upstream=call.upstream)
    runner.get(job.run_id, wait_seconds=5)
    runner.shutdown()

    # Upstream flows through, but the browser flags are inert defaults
    assert captured["upstream"]["research_ui"]["ui_mode"] == "visible"
    assert captured["upstream"]["research_ui"]["headless"] is False


def test_planner_launch_read_only_web_research_no_longer_sets_silent_upstream(
    tmp_path,
    monkeypatch,
):
    captured = {}
    drone = DroneDefinition(
        id=WEB_RESEARCH_DRONE_ID,
        name="Web Research",
        description="Probe",
        instructions="Probe",
        write_policy="read_only",
        output_contract={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "summary": {"type": "string"}},
            "required": ["ok", "summary"],
        },
        budget=DroneBudget(timeout_seconds=30),
        entrypoint={"kind": "command", "command": [sys.executable], "protocol": "json-stdio"},
    )

    class FakeRunner:
        def launch(self, launched_drone, goal, *, upstream=None):
            captured["drone"] = launched_drone
            captured["goal"] = goal
            captured["upstream"] = upstream
            return SimpleNamespace(
                run_id="run-1",
                status="running",
                drone_id=launched_drone.id,
                drone_name=launched_drone.name,
            )

    monkeypatch.setattr(
        "aura.drones.store.DroneStore.load_drone",
        lambda workspace_root, drone_id: drone,
    )
    monkeypatch.setattr(
        "aura.drones.background_runner.get_background_runner",
        lambda workspace_root: FakeRunner(),
    )
    registry = ToolRegistry(tmp_path, mode="planner")

    result = registry._handle_launch_read_only_drone(
        {
            "drone_id": WEB_RESEARCH_DRONE_ID,
            "goal": "Are there any World Cup matches today?",
        },
        approval_cb=None,
        reject_all=False,
    )

    assert result.ok is True
    # The upstream no longer has silent/headless browser flags
    assert "research_ui" not in captured["upstream"]
    # The research_request is present but without ui_mode/headless
    assert captured["upstream"]["research_request"]["question"] == "Are there any World Cup matches today?"
