from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from aura.conversation.tools.registry import ToolRegistry

SCRIPT = Path("scripts/personal/godot_vision/critique_godot_preview.py")


def _module():
    spec = importlib.util.spec_from_file_location("personal_godot_vision", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_personal_vision_tool_is_outside_packaged_aura() -> None:
    assert SCRIPT.parts[0] == "scripts"
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    build_script = Path("scripts/build_nuitka.py").read_text(encoding="utf-8")
    assert 'exclude = ["scripts*", "tests*"]' in pyproject
    assert "--include-package=aura" in build_script
    assert "personal/godot_vision" not in build_script
    assert not any(
        "critique_godot_preview_local" in path.read_text(encoding="utf-8", errors="ignore")
        for path in Path("aura").rglob("*.py")
    )


def test_personal_vision_tool_validates_path_and_calls_loopback_ollama(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    target = tmp_path / ".aura/tmp/godot_previews/pass/overview.png"
    target.parent.mkdir(parents=True)
    Image.new("RGB", (96, 64), (20, 30, 40)).save(target)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AURA_GODOT_VISION_MODEL", "local-vision")
    response = {"message": {"content": json.dumps({
        "verdict": "needs_revision",
        "reads_as": "Several fortification pieces near a possible road opening",
        "coherence_checks": {
            "single_place": "fail",
            "major_masses_connected": "fail",
            "primary_identity_clear": "unclear",
            "entrance_or_route_readable": "unclear",
            "spatial_logic_believable": "fail",
            "damage_and_rubble_causal": "unclear",
        },
        "critical_failures": [{
            "problem": "Detached wall masses",
            "evidence": "Large visible gaps separate the wall and tower silhouettes.",
            "impact": "The scene reads as kit pieces rather than one checkpoint.",
        }],
        "strongest_feature": "Unequal tower silhouettes",
        "next_revision": {
            "design_goal": "Unify the gatehouse mass",
            "visible_relationships": ["Connect both wall runs into the gate towers"],
        },
        "confidence": 0.9,
        "limitations": [],
    })}}

    with patch.object(module, "_ollama_chat", return_value=response) as chat:
        result = module.critique_godot_preview_local(
            ".aura/tmp/godot_previews/pass/overview.png",
            "Make a compact checkpoint",
            "one entrance; no overlaps",
        )

    assert result["ok"] is True
    assert result["local_only"] is True
    assert result["critique"]["verdict"] == "needs_revision"
    assert set(result["critique"]) == {
        "verdict",
        "reads_as",
        "coherence_checks",
        "critical_failures",
        "strongest_feature",
        "next_revision",
        "confidence",
        "limitations",
    }
    assert chat.call_args.args[0]["model"] == "local-vision"
    assert chat.call_args.args[0]["messages"][1]["images"]
    assert chat.call_args.args[0]["format"]["properties"]["verdict"]["enum"] == [
        "cannot_judge",
        "coherent",
        "needs_revision",
    ]

    escaped = module.critique_godot_preview_local("../outside.png", "request")
    assert escaped["ok"] is False


@pytest.mark.parametrize("verdict", ["coherent", "needs_revision", "cannot_judge"])
def test_personal_vision_normalizer_accepts_stable_verdicts(verdict: str) -> None:
    module = _module()
    result = module._normalize_critique({
        "verdict": verdict,
        "coherence_checks": {name: "pass" for name in module._CHECK_NAMES},
    })

    assert result["verdict"] == verdict
    assert tuple(result["coherence_checks"]) == module._CHECK_NAMES
    assert set(result["coherence_checks"].values()) <= {"pass", "fail", "unclear"}


def test_personal_vision_normalizer_bounds_failures_and_output() -> None:
    module = _module()
    result = module._normalize_critique({
        "verdict": "needs_revision",
        "coherence_checks": {},
        "critical_failures": [
            {"problem": f"failure {index}", "evidence": "x" * 2_000, "impact": "bad"}
            for index in range(5)
        ],
        "next_revision": {
            "design_goal": "Connect the primary masses",
            "visible_relationships": [f"relationship {index}" for index in range(8)],
        },
        "confidence": 4,
    })

    assert len(result["critical_failures"]) == 3
    assert result["critical_failures"][0]["evidence"].endswith("...[truncated]")
    assert len(result["next_revision"]["visible_relationships"]) == 4
    assert result["confidence"] == 1.0


def test_personal_vision_normalizer_makes_model_text_subprocess_safe() -> None:
    module = _module()
    result = module._normalize_critique({
        "verdict": "needs_revision",
        "reads_as": "A builder\u2019s scattered kit - caf\u00e9",
        "coherence_checks": {},
    })

    assert result["reads_as"] == "A builder's scattered kit - caf?"
    assert result["reads_as"].isascii()


def test_personal_vision_normalizes_malformed_and_contradictory_model_json() -> None:
    module = _module()
    malformed = module._parse_and_normalize_critique("not json")
    contradictory = module._normalize_critique({
        "verdict": "coherent",
        "coherence_checks": {"single_place": "fail"},
        "critical_failures": ["Scattered modular pieces"],
        "confidence": "unknown",
    })
    partial = module._normalize_critique({"reads_as": "A vague cluster"})

    assert malformed["verdict"] == "cannot_judge"
    assert "non-JSON" in malformed["limitations"][0]
    assert contradictory["verdict"] == "needs_revision"
    assert contradictory["confidence"] == 0.0
    assert partial["verdict"] == "cannot_judge"
    assert all(value == "unclear" for value in partial["coherence_checks"].values())


def test_personal_vision_prompt_reviews_environment_coherence_not_checklist_items() -> None:
    module = _module()
    prompt = " ".join(module._SYSTEM_PROMPT.lower().split())

    assert "one intentionally constructed" in prompt
    assert "unrelated pieces placed near one another" in prompt
    assert "scattered kit pieces" in prompt
    assert "disconnected primary masses" in prompt
    assert "walls form meaningful runs" in prompt
    assert "entrance or route" in prompt
    assert "rubble" in prompt and "collapse source" in prompt
    assert "explicitly reject" in prompt
    assert "merely contains requested modular pieces" in prompt
    assert "exact transforms" in prompt


def test_personal_vision_tool_is_worker_only_and_executes_through_registry(tmp_path: Path) -> None:
    tools_dir = tmp_path / ".aura/tools"
    tools_dir.mkdir(parents=True)
    shutil.copy2(SCRIPT, tools_dir / SCRIPT.name)
    worker = ToolRegistry(tmp_path, mode="worker")
    planner = ToolRegistry(tmp_path, mode="planner")

    worker_names = {tool["function"]["name"] for tool in worker.tool_defs()}
    planner_names = {tool["function"]["name"] for tool in planner.tool_defs()}
    result = worker.execute(
        "critique_godot_preview_local",
        {"capture_path": "../outside.png", "user_request": "Inspect the composition"},
        None,
    )

    assert "critique_godot_preview_local" in worker_names
    assert "critique_godot_preview_local" not in planner_names
    assert result.ok is True
    assert result.payload["result"] == {
        "ok": False,
        "local_only": True,
        "error": "capture_path must be a workspace-relative PNG",
    }
