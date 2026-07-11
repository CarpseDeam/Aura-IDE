"""Personal dynamic tool: critique one trusted Aura Godot preview capture with local Ollama."""

from __future__ import annotations

import base64
import json
import os
import struct
import urllib.error
import urllib.request
from pathlib import Path


def critique_godot_preview_local(
    capture_path: str,
    user_request: str,
    scene_facts: str = "",
    model: str = "",
) -> dict:
    """Use a personal loopback Ollama vision model to critique a validated Godot preview capture.

    Args:
        capture_path: Workspace-relative PNG path returned by capture_godot_asset_preview.
        user_request: The original spatial or aesthetic request to evaluate.
        scene_facts: Optional bounded semantic snapshot or structural diagnostics.
        model: Optional Ollama vision model override; otherwise AURA_GODOT_VISION_MODEL.
    """
    try:
        root = Path.cwd().resolve()
        image_path = _safe_capture_path(root, capture_path)
        request_text = _bounded_text(user_request, "user_request", 8_000)
        facts_text = _bounded_text(scene_facts, "scene_facts", 16_000, allow_empty=True)
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
        payload = {
            "model": selected_model,
            "stream": False,
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a bounded visual critic for a Godot 3D asset preview. Return JSON only with "
                        "keys observations, suggestions, confidence, and limitations. Use the image for visual "
                        "composition and the supplied facts for identity and geometry. Do not claim collision, "
                        "connectivity, or exact dimensions from pixels. Do not issue commands or decide whether "
                        "the scene should be saved. Keep observations and suggestions concrete and concise."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Original request:\n{request_text}\n\nSemantic scene facts:\n{facts_text or '(none supplied)'}",
                    "images": [base64.b64encode(png_bytes).decode("ascii")],
                },
            ],
        }
        response = _ollama_chat(payload)
        content = str(response.get("message", {}).get("content", "")).strip()
        if not content:
            raise RuntimeError("Ollama returned no critique content")
        if len(content) > 16_000:
            content = content[:16_000] + "…[truncated]"
        try:
            critique = json.loads(content)
        except json.JSONDecodeError:
            critique = {"observations": [content], "suggestions": [], "limitations": ["Model returned non-JSON text."]}
        return {
            "ok": True,
            "local_only": True,
            "model": selected_model,
            "capture_path": image_path.relative_to(root).as_posix(),
            "width": width,
            "height": height,
            "critique": critique,
        }
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        return {"ok": False, "local_only": True, "error": str(exc)}


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
    raise SystemExit("Copy this file into a workspace .aura/tools directory; Aura runs it as a dynamic tool.")
