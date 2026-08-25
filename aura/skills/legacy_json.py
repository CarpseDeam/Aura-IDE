"""Legacy flat-JSON skill compatibility, owned solely by SkillLibrary.

Before the SKILL.md folder format, authored skills lived as flat ``.json``
files under ``.aura/skills/authored/`` — either one object or a list of
objects. This is the one adapter that still reads that shape; it never
mutates the files it reads and never writes a new one.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aura.skills.diagnostics import SkillDiagnostic, error
from aura.skills.identity import is_valid_skill_name, normalize_skill_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegacySkillEntry:
    name: str
    text: str
    task_kinds: tuple[str, ...]
    path_globs: tuple[str, ...]
    model: str | None
    triggers: tuple[str, ...]
    description: str | None
    origin: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def read_legacy_json_skills(directory: Path) -> tuple[list[LegacySkillEntry], list[SkillDiagnostic]]:
    """Read every ``*.json`` file directly inside *directory*.

    Each file may hold one skill object or a list of skill objects. A file
    that fails to parse, or an item missing ``text``, is reported as a
    diagnostic and skipped — it never aborts the rest of discovery.
    """
    entries: list[LegacySkillEntry] = []
    diagnostics: list[SkillDiagnostic] = []
    if not directory.is_dir():
        return entries, diagnostics

    for entry in sorted(directory.iterdir(), key=lambda path: path.name):
        if entry.suffix != ".json":
            continue
        try:
            raw = entry.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as exc:
            diagnostics.append(error("legacy_json_unreadable", f"could not parse: {exc}", str(entry)))
            continue

        if isinstance(data, dict):
            items: list[Any] = [data]
        elif isinstance(data, list):
            items = data
        else:
            diagnostics.append(
                error("legacy_json_shape", "expected a JSON object or list of objects", str(entry))
            )
            continue

        multiple = len(items) > 1
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                diagnostics.append(
                    error("legacy_json_item_shape", f"item {index} is not a JSON object", str(entry))
                )
                continue
            text = str(item.get("text", "") or "").strip()
            if not text:
                diagnostics.append(
                    error("legacy_json_missing_text", f"item {index} has no 'text'", str(entry))
                )
                continue

            name = _derive_name(item, entry, index, multiple)
            raw_origin = item.get("origin", [])
            origin = (
                tuple(tuple(pair) for pair in raw_origin)
                if isinstance(raw_origin, list)
                else ()
            )
            entries.append(
                LegacySkillEntry(
                    name=name,
                    text=text,
                    task_kinds=_str_tuple(item.get("task_kinds")),
                    path_globs=_str_tuple(item.get("path_globs")),
                    model=(str(item["model"]).strip() or None) if item.get("model") else None,
                    triggers=_str_tuple(item.get("triggers")),
                    description=_description(item),
                    origin=origin,
                )
            )
    return entries, diagnostics


def _derive_name(item: dict[str, Any], entry: Path, index: int, multiple: bool) -> str:
    for key in ("name", "id", "skill_id"):
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate.strip():
            normalized = normalize_skill_name(candidate)
            if is_valid_skill_name(normalized):
                return normalized
    base = normalize_skill_name(entry.stem)
    return f"{base}-{index}" if multiple else base


def _str_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())


def _description(item: dict[str, Any]) -> str | None:
    raw = item.get("description")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None
