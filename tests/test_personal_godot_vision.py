from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from unittest.mock import patch

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
    response = {"message": {"content": json.dumps({"observations": ["clear silhouette"]})}}

    with patch.object(module, "_ollama_chat", return_value=response) as chat:
        result = module.critique_godot_preview_local(
            ".aura/tmp/godot_previews/pass/overview.png",
            "Make a compact checkpoint",
            "one entrance; no overlaps",
        )

    assert result["ok"] is True
    assert result["local_only"] is True
    assert result["critique"]["observations"] == ["clear silhouette"]
    assert chat.call_args.args[0]["model"] == "local-vision"
    assert chat.call_args.args[0]["messages"][1]["images"]

    escaped = module.critique_godot_preview_local("../outside.png", "request")
    assert escaped["ok"] is False


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
