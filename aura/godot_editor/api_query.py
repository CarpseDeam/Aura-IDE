"""Offline ClassDB querying through Aura's configured Godot executable."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from aura.godot_toolchain import resolve_godot_executable

OUTPUT_PREFIX = "AURA_GODOT_API_JSON:"


def query_godot_api_offline(project_root: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Run the same bounded ClassDB query without requiring the editor bridge."""
    root = project_root.resolve()
    resolution = resolve_godot_executable(root)
    if resolution.path is None:
        raise RuntimeError(resolution.message)
    runner = Path(__file__).parents[1] / "validation" / "godot_api_query.gd"
    command = [
        str(resolution.path),
        "--headless",
        "--path",
        str(root),
        "--script",
        str(runner),
        "--",
        json.dumps(params, separators=(",", ":")),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"configured Godot API query failed to start: {exc}") from exc
    output = completed.stdout + "\n" + completed.stderr
    payload_text = next(
        (line[len(OUTPUT_PREFIX) :] for line in output.splitlines() if line.startswith(OUTPUT_PREFIX)),
        "",
    )
    if not payload_text:
        detail = output.strip()[-2_000:] or f"Godot exited with code {completed.returncode}"
        raise RuntimeError(f"configured Godot API query returned no result: {detail}")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("configured Godot API query returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        error = payload.get("error") if isinstance(payload, dict) else "invalid result"
        raise RuntimeError(str(error or "configured Godot API query failed"))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("configured Godot API query returned an invalid result")
    return result


__all__ = ["query_godot_api_offline"]
