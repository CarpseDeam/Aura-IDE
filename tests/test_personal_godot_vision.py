from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from aura.conversation.tools.dynamic import (
    dynamic_tool_timeout_seconds,
    execute_dynamic_tool,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.sandbox import SandboxResult

SCRIPT = Path("scripts/personal/godot_vision/describe_godot_preview.py")
OLD_SCRIPT = Path("scripts/personal/godot_vision/critique_godot_preview.py")


def _module():
    spec = importlib.util.spec_from_file_location("personal_godot_vision", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Isolation and packaging
# ---------------------------------------------------------------------------


def test_descriptor_remains_outside_packaged_aura() -> None:
    assert SCRIPT.parts[0] == "scripts"
    assert not OLD_SCRIPT.exists(), "critique_godot_preview.py must be removed"
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    build_script = Path("scripts/build_nuitka.py").read_text(encoding="utf-8")
    assert 'exclude = ["scripts*", "tests*"]' in pyproject
    assert "--include-package=aura" in build_script
    assert "personal/godot_vision" not in build_script
    assert not any(
        "describe_godot_preview_local" in path.read_text(encoding="utf-8", errors="ignore")
        or "critique_godot_preview_local" in path.read_text(encoding="utf-8", errors="ignore")
        for path in Path("aura").rglob("*.py")
    )


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_descriptor_timeout_hint_is_scoped_and_wired(tmp_path: Path) -> None:
    ordinary_tool = tmp_path / "ordinary.py"
    ordinary_tool.write_text("def ordinary():\n    return True\n", encoding="utf-8")

    assert dynamic_tool_timeout_seconds(ordinary_tool) == 30
    assert dynamic_tool_timeout_seconds(SCRIPT) == 45

    with patch("aura.conversation.tools.dynamic.SandboxExecutor") as sandbox_type:
        sandbox_type.return_value.run_dynamic_tool.return_value = SandboxResult(
            ok=True,
            stdout='{"ok": true, "result": {}}',
            stderr="",
            exit_code=0,
        )
        result = execute_dynamic_tool(
            SCRIPT,
            "describe_godot_preview_local",
            {"capture_path": "capture.png"},
            tmp_path,
        )

    assert result["ok"] is True
    assert sandbox_type.return_value.run_dynamic_tool.call_args.kwargs["timeout"] == 45


@pytest.mark.parametrize("hint", ["0", "121", "True", "'45'", "unknown_name"])
def test_dynamic_tool_timeout_hint_rejects_invalid_values(tmp_path: Path, hint: str) -> None:
    tool = tmp_path / "invalid_timeout.py"
    tool.write_text(
        f"AURA_DYNAMIC_TOOL_TIMEOUT_SECONDS = {hint}\ndef tool():\n    return True\n",
        encoding="utf-8",
    )

    assert dynamic_tool_timeout_seconds(tool) == 30


# ---------------------------------------------------------------------------
# Happy path: PNG → Ollama → description
# ---------------------------------------------------------------------------


def test_descriptor_sends_png_and_returns_description(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    target = tmp_path / ".aura/tmp/godot_previews/pass/overview.png"
    target.parent.mkdir(parents=True)
    Image.new("RGB", (96, 64), (20, 30, 40)).save(target)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AURA_GODOT_VISION_MODEL", "local-vision")
    response = {
        "message": {
            "content": (
                "A ruined stone gateway with two unequal tower masses. "
                "Connected defensive walls run left and right. "
                "A clear central opening, rubble at the base of the left tower."
            )
        }
    }

    with patch.object(module, "_ollama_chat", return_value=response) as chat:
        result = module.describe_godot_preview_local(
            ".aura/tmp/godot_previews/pass/overview.png",
            "build a ruined checkpoint",
        )

    assert result["ok"] is True
    assert result["local_only"] is True
    assert result["model"] == "local-vision"
    assert result["capture_path"] == ".aura/tmp/godot_previews/pass/overview.png"
    assert result["width"] == 96
    assert result["height"] == 64
    assert "ruined stone gateway" in result["description"]
    assert "description" in result

    # Payload checks
    payload = chat.call_args.args[0]
    assert payload["model"] == "local-vision"
    assert payload["messages"][1]["images"]
    assert "format" not in payload  # no JSON schema enforcement


# ---------------------------------------------------------------------------
# Description result shape
# ---------------------------------------------------------------------------


def test_descriptor_result_contains_only_expected_keys(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".aura/tmp/godot_previews/keys.png"
    target.parent.mkdir(parents=True)
    Image.new("RGB", (80, 80)).save(target)

    with patch.object(module, "_ollama_chat", return_value={"message": {"content": "A stone wall."}}):
        result = module.describe_godot_preview_local(".aura/tmp/godot_previews/keys.png")

    assert set(result.keys()) == {
        "ok", "local_only", "model", "capture_path", "width", "height", "description",
    }


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_descriptor_rejects_path_escape() -> None:
    module = _module()
    escaped = module.describe_godot_preview_local("../outside.png")
    assert escaped["ok"] is False


def test_descriptor_reports_missing_file(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".aura/tmp/godot_previews").mkdir(parents=True)
    result = module.describe_godot_preview_local(".aura/tmp/godot_previews/missing.png")
    assert result["ok"] is False
    assert "does not exist" in result["error"]


def test_descriptor_reports_invalid_png(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    monkeypatch.chdir(tmp_path)
    preview_dir = tmp_path / ".aura/tmp/godot_previews"
    preview_dir.mkdir(parents=True)
    (preview_dir / "not_a_png.png").write_bytes(b"not a real PNG file at all")
    result = module.describe_godot_preview_local(".aura/tmp/godot_previews/not_a_png.png")
    assert result["ok"] is False
    assert "valid PNG" in result["error"]


def test_descriptor_reports_empty_ollama_content(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".aura/tmp/godot_previews/empty.png"
    target.parent.mkdir(parents=True)
    Image.new("RGB", (80, 80)).save(target)
    with patch.object(module, "_ollama_chat", return_value={"message": {"content": ""}}):
        result = module.describe_godot_preview_local(".aura/tmp/godot_previews/empty.png")
    assert result["ok"] is False
    assert "no description" in result.get("error", "").lower()


def test_descriptor_reports_invalid_ollama_json(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".aura/tmp/godot_previews/invalid.png"
    target.parent.mkdir(parents=True)
    Image.new("RGB", (80, 80)).save(target)
    with patch.object(module, "_ollama_chat", side_effect=RuntimeError("Ollama returned invalid JSON")):
        result = module.describe_godot_preview_local(".aura/tmp/godot_previews/invalid.png")
    assert result["ok"] is False
    assert "invalid JSON" in result["error"]


def test_descriptor_reports_http_failure(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".aura/tmp/godot_previews/http.png"
    target.parent.mkdir(parents=True)
    Image.new("RGB", (80, 80)).save(target)
    with patch.object(module, "_ollama_chat", side_effect=RuntimeError("Ollama returned HTTP 500")):
        result = module.describe_godot_preview_local(".aura/tmp/godot_previews/http.png")
    assert result["ok"] is False
    assert "HTTP 500" in result["error"]


# ---------------------------------------------------------------------------
# Registry exposure: single/worker yes, planner no
# ---------------------------------------------------------------------------


def test_descriptor_exposed_in_single_and_worker_but_not_planner(tmp_path: Path) -> None:
    tools_dir = tmp_path / ".aura/tools"
    tools_dir.mkdir(parents=True)
    shutil.copy2(SCRIPT, tools_dir / SCRIPT.name)

    worker = ToolRegistry(tmp_path, mode="worker")
    planner = ToolRegistry(tmp_path, mode="planner")
    single = ToolRegistry(tmp_path, mode="single")

    worker_names = {tool["function"]["name"] for tool in worker.tool_defs()}
    planner_names = {tool["function"]["name"] for tool in planner.tool_defs()}
    single_names = {tool["function"]["name"] for tool in single.tool_defs()}

    assert "describe_godot_preview_local" in worker_names
    assert "describe_godot_preview_local" in single_names
    assert "describe_godot_preview_local" not in planner_names


def test_critique_tool_not_exposed_in_any_mode(tmp_path: Path) -> None:
    tools_dir = tmp_path / ".aura/tools"
    tools_dir.mkdir(parents=True)
    shutil.copy2(SCRIPT, tools_dir / SCRIPT.name)

    for mode in ("single", "worker", "planner"):
        registry = ToolRegistry(tmp_path, mode=mode)
        names = {tool["function"]["name"] for tool in registry.tool_defs()}
        assert "critique_godot_preview_local" not in names, f"exposed in {mode} mode"


# ---------------------------------------------------------------------------
# No critic concepts remain
# ---------------------------------------------------------------------------


def test_no_critic_vocabulary_in_source() -> None:
    """Verify no critic concepts exist as tool features, schemas, or return fields.

    The prompt may *forbid* critic actions (e.g. "do not issue a verdict"),
    but the tool must not implement critic behaviour or expose critic fields.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    # These critic concepts must not appear as features / fields / helpers
    forbidden_in_code = [
        "_VERDICTS",
        "_CHECK_NAMES",
        "_CHECK_VALUES",
        "_CRITIQUE_JSON_SCHEMA",
        "_normalize_critique",
        "_parse_and_normalize_critique",
        "_normalize_check",
        "needs_revision",
        "cannot_judge",
        "coherence_check",
        "critical_failure",
        "strongest_feature",
        "next_revision",
        "confidence",
    ]
    for word in forbidden_in_code:
        assert word not in source, f"Critic concept '{word}' found in source"


def test_no_critic_vocabulary_in_returned_payload(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".aura/tmp/godot_previews/payload.png"
    target.parent.mkdir(parents=True)
    Image.new("RGB", (80, 80)).save(target)

    with patch.object(module, "_ollama_chat", return_value={"message": {"content": "A stone wall."}}):
        result = module.describe_godot_preview_local(".aura/tmp/godot_previews/payload.png")

    result_keys = set(result.keys())
    forbidden = {
        "verdict", "reads_as", "coherence_checks", "critical_failures",
        "strongest_feature", "next_revision", "confidence", "limitations",
        "critique", "needs_revision", "coherent", "cannot_judge",
        "score", "assessment", "findings", "recommendations",
        "suggested_changes",
    }
    assert result_keys.isdisjoint(forbidden), f"Critic keys found in result: {result_keys & forbidden}"


# ---------------------------------------------------------------------------
# Description prompt is factual, not critical
# ---------------------------------------------------------------------------


def test_descriptor_prompt_is_factual_not_critical() -> None:
    module = _module()
    prompt = " ".join(module._SYSTEM_PROMPT.lower().split())

    # Must describe what to report
    assert "factual visual descriptor" in prompt
    assert "describe only what is visibly present" in prompt
    assert "pure evidence" in prompt
    assert "footprint" in prompt
    assert "wall runs" in prompt
    assert "openings" in prompt
    assert "towers" in prompt
    assert "rubble" in prompt
    assert "silhouette" in prompt
    assert "unfinished edges" in prompt
    assert "camera angle" in prompt
    assert "occluded" in prompt
    assert "uncertain" in prompt

    # Must forbid critic behaviour (these words appear in prohibitions, which is correct)
    assert "do not" in prompt or "never" in prompt
    assert "verdict" in prompt  # explicitly forbidden — "never … issue a verdict"
    assert "score" in prompt   # explicitly forbidden — "never … score coherence"

    # The prompt must explicitly forbid critic behaviours (these words should
    # appear in the "do not" section, not as instructions to perform)
    assert "checklist" in prompt    # forbidden — "Do not ... list checklist items"
    assert "explicitly reject" not in prompt
    # These signal the old critic's positive instruction pattern and must be absent
    assert "return json" not in prompt
    assert "matching the supplied schema" not in prompt
    assert "needs_revision" not in prompt
    assert "cannot_judge" not in prompt


# ---------------------------------------------------------------------------
# Text safety
# ---------------------------------------------------------------------------


def test_safe_text_normalizes_and_bounds() -> None:
    module = _module()
    result = module._safe_text("A builder’s scattered kit — café", 100)
    assert result == "A builder's scattered kit - caf?"
    assert result.isascii()

    long = module._safe_text("x" * 5_000, 200)
    assert len(long) <= 215  # 200 + "...[truncated]" (~13 chars)
    assert long.endswith("...[truncated]")
