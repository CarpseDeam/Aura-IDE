"""Personal dynamic tool: describe one trusted Aura Godot preview capture with local Ollama.

This tool sends a Godot preview screenshot to a locally running Ollama vision model
and returns a concise, factual visual description.  It does **not** judge quality, issue
verdicts, score coherence, or recommend changes — it is purely DeepSeek's local eyes.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import urllib.error
import urllib.request
from pathlib import Path

AURA_DYNAMIC_TOOL_TIMEOUT_SECONDS = 45

_SYSTEM_PROMPT = """You are a factual visual descriptor.  Describe only what is visibly present in
this Godot 3D editor preview screenshot so that another AI (DeepSeek) can continue constructing the
scene.  Your description is pure evidence — it must never judge quality, issue a verdict, score
coherence, recommend changes, select the next action, or act as a planner.

Describe concisely in plain English:
- Overall footprint and spatial layout
- Connected wall runs, corners, and structural lines
- Openings, doors, gates, breaches, and gaps
- Rooms, enclosed areas, and negative spaces
- Towers, vertical masses, and landmarks
- Overlaps, detached fragments, and isolated pieces
- Rubble, damage, and where it appears relative to structures
- Silhouette and dominant mass hierarchy
- Relative scale of major elements
- Obvious unfinished edges, missing sections, or disconnected ends
- Camera angle, framing, and any areas that are occluded, out of view, or uncertain

Do not:
- Judge whether the scene is good, coherent, or successful
- Issue a verdict or score
- Recommend what to build next
- Act as a planner or decision-maker
- List checklist items or pass/fail assessments
- Invent asset identities, node paths, exact transforms, or dimensions from pixels"""

_DESCRIPTION_LIMIT = 4_000


def describe_godot_preview_local(
    capture_path: str,
    scene_context: str = "",
    model: str = "",
) -> dict:
    """Use a personal loopback Ollama vision model to describe a validated Godot preview capture.

    Args:
        capture_path: Workspace-relative PNG path returned by capture_godot_asset_preview.
        scene_context: Optional brief scene context (current structural facts, brief, etc.).
        model: Optional Ollama vision model override; otherwise AURA_GODOT_VISION_MODEL.
    """
    try:
        root = Path.cwd().resolve()
        image_path = _safe_capture_path(root, capture_path)
        context_text = _bounded_text(scene_context, "scene_context", 8_000, allow_empty=True)
        selected_model = (
            model
            or os.environ.get("AURA_GODOT_VISION_MODEL", "")
            or "gemma3:12b"
        ).strip()
        if not selected_model or len(selected_model) > 200:
            raise ValueError(
                "Set AURA_GODOT_VISION_MODEL to a locally installed Ollama vision model."
            )
        png_bytes = image_path.read_bytes()
        width, height = _png_dimensions(png_bytes)

        user_content = "Describe this Godot 3D editor preview screenshot factually."
        if context_text:
            user_content += f"\n\nScene context (authoritative for identity, not appearance):\n{context_text}"

        payload = {
            "model": selected_model,
            "stream": False,
            "options": {"temperature": 0.1},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": user_content,
                    "images": [base64.b64encode(png_bytes).decode("ascii")],
                },
            ],
        }
        response = _ollama_chat(payload)
        content = str(response.get("message", {}).get("content", "")).strip()
        if not content:
            raise RuntimeError("Ollama returned no description content")

        description = _safe_text(content, _DESCRIPTION_LIMIT)

        return {
            "ok": True,
            "local_only": True,
            "model": selected_model,
            "capture_path": image_path.relative_to(root).as_posix(),
            "width": width,
            "height": height,
            "description": description,
        }
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        return {"ok": False, "local_only": True, "error": str(exc)}


# ---------------------------------------------------------------------------
# Safe helpers (shared infrastructure — kept from the original tool)
# ---------------------------------------------------------------------------


def _safe_text(value: str, limit: int) -> str:
    """Normalise text to safe ASCII and bound its length."""
    text = str(value or "").strip()
    text = (
        text.replace("‘", "'")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
        .replace("…", "...")
    )
    text = text.encode("ascii", errors="replace").decode("ascii")
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _safe_capture_path(root: Path, raw_path: str) -> Path:
    relative = Path(str(raw_path).strip())
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".png":
        raise ValueError("capture_path must be a workspace-relative PNG")
    allowed = (root / ".aura" / "tmp" / "godot_previews").resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(allowed)
    except ValueError as exc:
        raise ValueError("capture_path must stay beneath .aura/tmp/godot_previews") from exc
    if not candidate.is_file():
        raise ValueError("capture_path does not exist")
    if candidate.stat().st_size <= 0 or candidate.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("capture PNG size is outside the allowed range")
    return candidate


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise ValueError("capture is not a valid PNG header")
    width, height = struct.unpack(">II", payload[16:24])
    if not 64 <= width <= 1920 or not 64 <= height <= 1080:
        raise ValueError("capture PNG dimensions are outside the allowed range")
    return width, height


def _bounded_text(value: str, label: str, limit: int, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and not allow_empty:
        raise ValueError(f"{label} is required")
    if len(text) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    return text


def _ollama_chat(payload: dict) -> dict:
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 200:
                raise RuntimeError(f"Ollama returned HTTP {response.status}")
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(2_000).decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Ollama returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Ollama returned an invalid response object")
    return parsed


if __name__ == "__main__":
    raise SystemExit(
        "Copy this file into a workspace .aura/tools directory; Aura runs it as a dynamic tool."
    )
