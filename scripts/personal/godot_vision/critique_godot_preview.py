"""Personal dynamic tool: critique one trusted Aura Godot preview capture with local Ollama."""

from __future__ import annotations

import base64
import json
import os
import struct
import urllib.error
import urllib.request
from pathlib import Path


AURA_DYNAMIC_TOOL_TIMEOUT_SECONDS = 45
_VERDICTS = {"coherent", "needs_revision", "cannot_judge"}
_CHECK_NAMES = (
    "single_place",
    "major_masses_connected",
    "primary_identity_clear",
    "entrance_or_route_readable",
    "spatial_logic_believable",
    "damage_and_rubble_causal",
)
_CHECK_VALUES = {"pass", "fail", "unclear"}
_CRITIQUE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": sorted(_VERDICTS)},
        "reads_as": {"type": "string"},
        "coherence_checks": {
            "type": "object",
            "properties": {
                name: {"type": "string", "enum": sorted(_CHECK_VALUES)}
                for name in _CHECK_NAMES
            },
            "required": list(_CHECK_NAMES),
        },
        "critical_failures": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "problem": {"type": "string"},
                    "evidence": {"type": "string"},
                    "impact": {"type": "string"},
                },
                "required": ["problem", "evidence", "impact"],
            },
        },
        "strongest_feature": {"type": "string"},
        "next_revision": {
            "type": "object",
            "properties": {
                "design_goal": {"type": "string"},
                "visible_relationships": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {"type": "string"},
                },
            },
            "required": ["design_goal", "visible_relationships"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "limitations": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string"},
        },
    },
    "required": [
        "verdict",
        "reads_as",
        "coherence_checks",
        "critical_failures",
        "strongest_feature",
        "next_revision",
        "confidence",
        "limitations",
    ],
}
_SYSTEM_PROMPT = """You are a visual environment-construction reviewer for a Godot 3D preview.
Your central question is: Do these modular assets visually read as one intentionally constructed
place, or as unrelated pieces placed near one another? Give a plain verdict. This is not a beauty
score and visible checklist items are not evidence that the composition works.

Return JSON matching the supplied schema. Use verdict `needs_revision` whenever foundational visual
assembly fails: scattered kit pieces, disconnected primary masses, an illegible environment type,
an entrance or route that leads nowhere, unbelievable enclosure boundaries, rubble without a visible
collapse source, no dominant landmark, or large accidental gaps that break structural continuity.
Use `coherent` only when the broad environment reads as one intentional place and remaining problems
are secondary refinement. Use `cannot_judge` only when framing, angle, occlusion, or image quality
prevents a reliable decision.

Evaluate whether major masses connect; walls form meaningful runs and terminate at believable
corners, towers, buildings, gates, breaches, or collapse; detached fragments look intentionally
ruined rather than accidentally isolated; the requested primary identity and entrance or route are
immediately legible; interior, sheltered, and negative spaces have believable boundaries; towers and
landmarks belong to the same structure; rubble visibly originates from collapse; and the silhouette
has a dominant hierarchy. Explicitly reject a scene that merely contains requested modular pieces
without composing them into a location.

List at most three critical failures, ordered worst first. For each, state the visible problem, image
evidence, and why it harms the intended environment. Preserve the strongest feature. Recommend exactly
one focused next revision with concrete visible relationships such as closing a gap, connecting a wall
run into a tower, removing an isolated competing fragment, moving collapse debris toward its source,
or strengthening an entrance as the dominant center. Do not invent node paths, asset identities, exact
transforms, collision, dimensions, or hidden connectivity from pixels. Supplied scene facts are
authoritative for exact identity and geometry; the image is authoritative for visual coherence."""


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
            "format": _CRITIQUE_JSON_SCHEMA,
            "options": {"temperature": 0.1},
            "messages": [
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT,
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
        critique = _parse_and_normalize_critique(content)
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


def _parse_and_normalize_critique(content: str) -> dict:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        return _normalize_critique(
            {}, extra_limitations=["Model returned non-JSON text; no reliable visual verdict was available."]
        )
    if not isinstance(raw, dict):
        return _normalize_critique(
            {}, extra_limitations=["Model returned a non-object JSON value; no reliable visual verdict was available."]
        )
    return _normalize_critique(raw)


def _normalize_critique(raw: dict, *, extra_limitations: list[str] | None = None) -> dict:
    limitations = _string_list(raw.get("limitations"), max_items=5, item_limit=500)
    limitations.extend(extra_limitations or [])

    verdict = _text(raw.get("verdict"), 40).lower()
    if verdict not in _VERDICTS:
        verdict = "cannot_judge"
        limitations.append("Model returned a missing or invalid verdict; normalized to cannot_judge.")

    raw_checks = raw.get("coherence_checks")
    if not isinstance(raw_checks, dict):
        raw_checks = {}
    checks = {name: _normalize_check(raw_checks.get(name)) for name in _CHECK_NAMES}

    raw_failures = raw.get("critical_failures")
    if isinstance(raw_failures, str):
        raw_failures = [raw_failures]
    if not isinstance(raw_failures, list):
        raw_failures = []
    failures: list[dict[str, str]] = []
    for item in raw_failures[:3]:
        if isinstance(item, dict):
            failure = {
                "problem": _text(item.get("problem"), 600),
                "evidence": _text(item.get("evidence"), 900),
                "impact": _text(item.get("impact"), 700),
            }
        else:
            failure = {"problem": _text(item, 600), "evidence": "", "impact": ""}
        if any(failure.values()):
            failures.append(failure)

    next_revision = raw.get("next_revision")
    if isinstance(next_revision, dict):
        normalized_revision = {
            "design_goal": _text(next_revision.get("design_goal"), 700),
            "visible_relationships": _string_list(
                next_revision.get("visible_relationships"), max_items=4, item_limit=700
            ),
        }
    else:
        normalized_revision = {
            "design_goal": _text(next_revision, 700),
            "visible_relationships": [],
        }

    if verdict == "coherent" and ("fail" in checks.values() or failures):
        verdict = "needs_revision"
        limitations.append(
            "Model marked the scene coherent despite failed checks or critical failures; normalized to needs_revision."
        )
    elif verdict == "coherent" and all(value == "unclear" for value in checks.values()):
        verdict = "cannot_judge"
        limitations.append(
            "Model marked the scene coherent without any judgeable coherence check; normalized to cannot_judge."
        )

    confidence = raw.get("confidence", 0.0)
    try:
        normalized_confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        normalized_confidence = 0.0
        limitations.append("Model returned invalid confidence; normalized to 0.0.")

    return {
        "verdict": verdict,
        "reads_as": _text(raw.get("reads_as"), 1_000),
        "coherence_checks": checks,
        "critical_failures": failures,
        "strongest_feature": _text(raw.get("strongest_feature"), 800),
        "next_revision": normalized_revision,
        "confidence": normalized_confidence,
        "limitations": _dedupe(limitations)[:5],
    }


def _normalize_check(value: object) -> str:
    if isinstance(value, bool):
        return "pass" if value else "fail"
    normalized = _text(value, 40).lower().replace(" ", "_")
    aliases = {
        "passed": "pass",
        "true": "pass",
        "yes": "pass",
        "failed": "fail",
        "false": "fail",
        "no": "fail",
        "unknown": "unclear",
        "cannot_judge": "unclear",
        "not_visible": "unclear",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _CHECK_VALUES else "unclear"


def _text(value: object, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2026", "...")
    )
    text = text.encode("ascii", errors="replace").decode("ascii")
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _string_list(value: object, *, max_items: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [text for item in value[:max_items] if (text := _text(item, item_limit))]


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value, 500)
        if text and text not in result:
            result.append(text)
    return result


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
