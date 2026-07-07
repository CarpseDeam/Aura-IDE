"""Private type-coercion helpers for dispatch dataclasses."""
from __future__ import annotations

import re
from typing import Any


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _target_region_list(value: Any) -> list[dict[str, Any]]:
    """Coerce Planner-provided target regions into clean structured entries."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        region: dict[str, Any] = {}
        for key in ("path", "symbol", "note"):
            text = _target_region_text(item.get(key))
            if text:
                region[key] = text
        for key in ("start_line", "end_line"):
            line = _target_region_line(item.get(key))
            if line is not None:
                region[key] = line
        if region:
            result.append(region)
    return result


def _target_region_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _target_region_line(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        line = value
    elif isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"\d+", text):
            return None
        line = int(text)
    else:
        return None
    return line if line > 0 else None


def _require_list_str(value: Any, field: str = "") -> list[str]:
    """Coerce a value to a list of strings.

    If value is a list, each item is coerced to str.
    If value is a single string, wraps it in a list.
    Otherwise returns an empty list.
    """
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _string_dict_list(value: Any) -> dict[str, list[str]]:
    """Safely coerce expected_dataclass_fields to dict[str, list[str]].

    If value is a dict, normalizes each value to list[str].
    If value is a list (old format) or None/missing, returns {}.
    """
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, val in value.items():
        if isinstance(val, list):
            result[str(key)] = [str(v) for v in val]
        else:
            result[str(key)] = []
    return result


def _extract_validation_commands(text: str) -> list[str]:
    """Extract validation commands from acceptance text.

    Uses two strategies:
    1. Backtick-quoted commands — everything between backticks that
       starts with a known command prefix.
    2. Full-line command forms — lines that start with a known command
       prefix after stripping leading bullets/whitespace.

    Does NOT scrape partial commands from prose sentences.
    Deduplicates while preserving order.
    """
    commands: list[str] = []
    seen: set[str] = set()

    _cmd_prefix = r"(?:python -m|pytest|python|ruff|mypy|py_compile|compileall)"

    # 1. Backtick-quoted commands — capture everything between backticks.
    for m in re.finditer(
        rf"`((?:{_cmd_prefix})\s+\S[^`]*)`",
        text,
    ):
        cmd = m.group(1).strip()
        if cmd not in seen:
            seen.add(cmd)
            commands.append(cmd)

    # 2. Full-line command forms.
    for line in text.splitlines():
        stripped = line.strip()
        while stripped and stripped[0] in "-* ":
            stripped = stripped[1:].lstrip()
        if re.match(rf"{_cmd_prefix}\s", stripped):
            # Strip a single trailing sentence-ending period.
            if stripped.endswith(".") and len(stripped) > 1 and stripped[-2].isalpha():
                stripped = stripped[:-1]
            cmd = stripped
            if cmd not in seen:
                seen.add(cmd)
                commands.append(cmd)

    return commands


def _str_list_items(data: dict[str, Any], key: str) -> list[str]:
    raw = data.get(key)
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _validation_spec_list(data: dict[str, Any], key: str) -> list:
    """Coerce raw validation_commands data into a list of ValidationCommandSpec.

    Supports both the new structured format (list of dicts) and legacy
    flat-string format (list of strings) for backward compatibility.

    Note: return type is plain ``list`` to avoid a circular import of
    ``ValidationCommandSpec`` at module level.
    """
    raw = data.get(key)
    if not isinstance(raw, list):
        return []
    # Lazy import to break circular dependency:
    # _dispatch_helpers → work_artifact.model → work_artifact.controller → dispatch
    from aura.work_artifact.model import ValidationCommandSpec  # noqa: PLC0415
    return [ValidationCommandSpec.from_dict(v) for v in raw]


def _none_or_str(value: Any) -> str | None:
    return str(value) if value is not None else None
