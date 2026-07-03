import json
import sys
from aura.drones.definition import DroneBudget, DroneDefinition
from aura.drones.folder_runner import run_folder_drone_sync
from aura.research.adapter import WEB_RESEARCH_DRONE_ID, build_adapter_call
from aura.research.request import build_research_request


def test_answer_only_adapter_call_requests_silent_headless_research():
    request = build_research_request("Are there any World Cup matches today?")

    call = build_adapter_call(request)

    assert call.drone_id == WEB_RESEARCH_DRONE_ID
    assert call.goal == "Are there any World Cup matches today?"
    assert call.upstream["research_ui"]["ui_mode"] == "silent"
    assert call.upstream["research_ui"]["headless"] is True
    assert call.upstream["research_ui"]["visible"] is False
    assert call.upstream["headless"] is True


def test_web_research_folder_runner_passes_silent_intent_in_payload_and_env(
    tmp_path,
    monkeypatch,
):
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
                "        'route_used': {'browser_discovery': {'browser_visible': False}},",
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
    assert payload["upstream"]["research_ui"]["ui_mode"] == "silent"
    assert payload["upstream"]["research_ui"]["headless"] is True
    assert payload["upstream"]["research_ui"]["visible"] is False
    assert env["AURA_RESEARCH_UI_MODE"] == "silent"
    assert env["AURA_WEB_RESEARCH_HEADLESS"] == "1"
    assert env["AURA_WEB_RESEARCH_VISIBLE"] == "0"
