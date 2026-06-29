from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aura.skills.models import Skill, SkillProvenance

logger = logging.getLogger(__name__)

try:
    from aura.hazard.guard_text import format_guard_line
    from aura.hazard.reader import GraduatedHazard, read_graduated
except ImportError:
    GraduatedHazard = None  # type: ignore[assignment,misc]
    format_guard_line = None  # type: ignore[assignment]

    def read_graduated(*args, **kwargs) -> list:  # type: ignore[misc]
        return []


def _read_bundled_skills() -> list[Skill]:
    """Read bundled skill JSON files from the bundled package directory."""
    skills: list[Skill] = []
    try:
        import importlib.resources as ilr

        try:
            pkg_files = ilr.files("aura.skills.bundled")
        except (ModuleNotFoundError, TypeError, Exception):
            pkg_files = None

        if pkg_files is not None and pkg_files.is_dir():
            entries = list(pkg_files.iterdir())
        else:
            # Fallback: compute from __file__
            bundled_dir = Path(__file__).resolve().parent / "bundled"
            if bundled_dir.is_dir():
                entries = list(bundled_dir.iterdir())
            else:
                entries = []

        for entry in entries:
            if entry.suffix != ".json":
                continue
            try:
                if isinstance(entry, Path):
                    raw = entry.read_text(encoding="utf-8")
                else:
                    raw = entry.read_bytes()
                data = json.loads(raw) if isinstance(raw, str) else json.loads(raw)
            except Exception:
                logger.debug("Failed to read bundled skill %s", entry, exc_info=True)
                continue
            if not isinstance(data, dict):
                continue
            text = data.get("text", "")
            if not text:
                continue
            task_kinds = tuple(data.get("task_kinds", []) or [])
            path_globs = tuple(data.get("path_globs", []) or [])
            model = data.get("model", None)
            skills.append(
                Skill(
                    text=text,
                    task_kinds=task_kinds,
                    path_globs=path_globs,
                    model=model,
                    provenance=SkillProvenance.BUNDLED,
                    origin=(),
                )
            )
    except Exception:
        logger.debug("Failed to read bundled skills", exc_info=True)
    return skills



def _read_graduated_skills(
    workspace_root: str | Path,
    *,
    window_days: int = 30,
) -> list[Skill]:
    """Read graduated hazards and adapt them into Skill objects."""
    if GraduatedHazard is None:
        return []
    skills: list[Skill] = []
    try:
        hazards = read_graduated(workspace_root, window_days=window_days)
    except Exception:
        logger.debug("Failed to read graduated hazards", exc_info=True)
        return []
    for h in hazards:
        try:
            task_kinds = (h.task_kind,) if h.task_kind is not None else ()
            path_globs = tuple(h.sample_target_files or ())
            model = h.model
            text = format_guard_line(h)
            origin = (
                ("fingerprint", h.fingerprint),
                ("distinct_dispatch_count", str(h.distinct_dispatch_count)),
                ("last_seen", h.last_seen),
            )
            skills.append(
                Skill(
                    text=text,
                    task_kinds=task_kinds,
                    path_globs=path_globs,
                    model=model,
                    provenance=SkillProvenance.FAILURE_GRADUATED,
                    origin=origin,
                )
            )
        except Exception:
            logger.debug("Failed to adapt graduated hazard", exc_info=True)
            continue
    return skills


def _read_refined_skills(workspace_root: str | Path) -> list[Skill]:
    """Read refined skill JSON files from .aura/skills/refined/."""
    try:
        refined_dir = Path(workspace_root) / ".aura" / "skills" / "refined"
        if not refined_dir.is_dir():
            return []

        skills: list[Skill] = []
        for entry in sorted(refined_dir.iterdir()):
            if entry.suffix != ".json":
                continue
            try:
                raw = entry.read_text(encoding="utf-8")
                data = json.loads(raw)
            except Exception:
                logger.debug("Failed to read refined skill %s", entry, exc_info=True)
                continue
            if not isinstance(data, dict):
                continue
            text = data.get("text", "")
            if not text:
                continue
            task_kinds = tuple(data.get("task_kinds", []) or [])
            path_globs = tuple(data.get("path_globs", []) or [])
            model = data.get("model", None)
            raw_origin = data.get("origin", [])
            if isinstance(raw_origin, list):
                origin = tuple(tuple(pair) for pair in raw_origin)
            else:
                origin = ()
            skills.append(
                Skill(
                    text=text,
                    task_kinds=task_kinds,
                    path_globs=path_globs,
                    model=model,
                    provenance=SkillProvenance.REFLECTION_REFINED,
                    origin=origin,
                )
            )
        return skills
    except Exception:
        logger.debug("Failed to read refined skills", exc_info=True)
        return []


def _metadata_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = data.get(key, [])
    if not isinstance(raw, list):
        return ()
    return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())


def _metadata_model(data: dict[str, Any]) -> str | None:
    raw = data.get("model", None)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _parse_front_matter_value(value: str) -> Any:
    text = value.strip()
    if text == "":
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text.strip("\"'")


def _parse_skill_markdown(raw: str, skill_path: Path) -> tuple[str, dict[str, Any]]:
    text = raw.strip()
    if not text:
        return "", {}
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return text, {}

    end_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        stripped = line.strip()
        if len(stripped) >= 3 and set(stripped) == {"-"}:
            end_index = index
            break
    if end_index is None:
        logger.debug("Malformed authored skill metadata in %s: missing closing delimiter", skill_path)
        return "", {}

    metadata: dict[str, Any] = {}
    try:
        for line in lines[1:end_index]:
            stripped = line.strip()
            if not stripped:
                continue
            if ":" not in stripped:
                raise ValueError(f"metadata line missing ':' in {skill_path}")
            key, value = stripped.split(":", 1)
            key = key.strip()
            if key not in {"task_kinds", "path_globs", "model", "triggers"}:
                continue
            metadata[key] = _parse_front_matter_value(value)
    except Exception:
        logger.debug("Failed to parse authored skill metadata %s", skill_path, exc_info=True)
        metadata = {}

    body = "\n".join(lines[end_index + 1:]).strip()
    return body, metadata


def _read_user_authored_skills(workspace_root: str | Path) -> list[Skill]:
    """Read user-authored skills from .aura/skills/authored/.

    Primary format: .aura/skills/authored/<skill_id>/SKILL.md.
    Compatibility format: flat .json files with one object or an object list.
    """
    try:
        authored_dir = Path(workspace_root) / ".aura" / "skills" / "authored"
        if not authored_dir.is_dir():
            return []

        skills: list[Skill] = []
        for entry in sorted(authored_dir.iterdir(), key=lambda path: path.name):
            if entry.is_dir():
                skill_path = entry / "SKILL.md"
                if not skill_path.is_file():
                    continue
                try:
                    raw = skill_path.read_text(encoding="utf-8")
                    text, metadata = _parse_skill_markdown(raw, skill_path)
                except Exception:
                    logger.debug("Failed to read user-authored skill %s", skill_path, exc_info=True)
                    continue
                if not text:
                    continue
                skills.append(
                    Skill(
                        text=text,
                        task_kinds=_metadata_list(metadata, "task_kinds"),
                        path_globs=_metadata_list(metadata, "path_globs"),
                        model=_metadata_model(metadata),
                        provenance=SkillProvenance.USER_AUTHORED,
                        origin=(("skill_id", entry.name),),
                        triggers=_metadata_list(metadata, "triggers"),
                    )
                )
                continue

            if entry.suffix != ".json":
                continue
            try:
                raw = entry.read_text(encoding="utf-8")
                data = json.loads(raw)
            except Exception:
                logger.debug("Failed to read user-authored skill %s", entry, exc_info=True)
                continue
            if isinstance(data, dict):
                items = (data,)
            elif isinstance(data, list):
                items = data
            else:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                task_kinds = tuple(item.get("task_kinds", []) or [])
                path_globs = tuple(item.get("path_globs", []) or [])
                model = item.get("model", None)
                triggers = tuple(item.get("triggers", []) or [])
                raw_origin = item.get("origin", [])
                if isinstance(raw_origin, list):
                    origin = tuple(tuple(pair) for pair in raw_origin)
                else:
                    origin = ()
                skills.append(
                    Skill(
                        text=text,
                        task_kinds=task_kinds,
                        path_globs=path_globs,
                        model=model,
                        provenance=SkillProvenance.USER_AUTHORED,
                        origin=origin,
                        triggers=triggers,
                    )
                )
        return skills
    except Exception:
        logger.debug("Failed to read user-authored skills", exc_info=True)
        return []


def read_skills(
    workspace_root: str | Path,
    *,
    window_days: int = 30,
) -> list[Skill]:
    """Read all skills: authored first, then bundled, graduated, and refined.

    Returns empty list on any failure — never propagates exceptions.
    """
    try:
        authored = _read_user_authored_skills(workspace_root)
        bundled = _read_bundled_skills()
        graduated = _read_graduated_skills(workspace_root, window_days=window_days)
        refined = _read_refined_skills(workspace_root)
        return authored + bundled + graduated + refined
    except Exception:
        logger.debug("read_skills failed", exc_info=True)
        return []
