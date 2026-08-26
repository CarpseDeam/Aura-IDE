from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aura.skills.description import derive_skill_description
from aura.skills.diagnostics import SkillDiagnostic
from aura.skills.library import SkillLibrary
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


def _read_graduated_skills(
    workspace_root: str | Path,
    *,
    window_days: int = 30,
) -> list[Skill]:
    """Read graduated hazards and adapt them into Skill objects.

    Graduated hazards are internal runtime guards, not installable user
    skills — they never pass through SkillLibrary's discovery or lifecycle
    operations.
    """
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
                    description=derive_skill_description(text),
                )
            )
        except Exception:
            logger.debug("Failed to adapt graduated hazard", exc_info=True)
            continue
    return skills


def _metadata_description(data: dict[str, Any]) -> str | None:
    """Authoritative ``description`` metadata value, or None when malformed.

    Only a non-empty string counts; any other declared shape (list, number,
    object, empty string) fails closed to None so the deterministic body
    fallback is used instead.  A malformed value must never crash composition.
    """
    raw = data.get("description", None)
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _read_refined_skills(workspace_root: str | Path) -> list[Skill]:
    """Read refined skill JSON files from .aura/skills/refined/.

    Reflection-refined guards are internal runtime guards, not installable
    user skills — they never pass through SkillLibrary's discovery or
    lifecycle operations.
    """
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
                    description=derive_skill_description(
                        text,
                        explicit=_metadata_description(data),
                    ),
                )
            )
        return skills
    except Exception:
        logger.debug("Failed to read refined skills", exc_info=True)
        return []


def _report_discovery_diagnostics(diagnostics: list[SkillDiagnostic]) -> None:
    """Log what SkillLibrary found wrong, instead of dropping it on the floor.

    An installed skill that fails to load is a real, fixable problem the user
    owns, so it belongs in the log the same way every other skills-package
    failure does. It does *not* belong in the provider prompt: paths and
    parser messages are operator diagnostics, and the composed prompt only
    ever carries skill bodies. The structured objects stay available to
    management callers through
    :meth:`aura.skills.library.SkillLibrary.list_installed`.
    """
    for diagnostic in diagnostics:
        if diagnostic.is_error:
            logger.warning(
                "Installed skill excluded (%s): %s [%s]", diagnostic.code, diagnostic.message, diagnostic.path
            )
        else:
            logger.debug(
                "Installed skill warning (%s): %s [%s]", diagnostic.code, diagnostic.message, diagnostic.path
            )


def read_skills(
    workspace_root: str | Path,
    *,
    window_days: int = 30,
) -> list[Skill]:
    """Read all skills: installed (via SkillLibrary), then graduated, then refined.

    SkillLibrary is the sole owner of installed project/personal/bundled
    skill discovery — this function is only the runtime aggregation boundary
    that adds the two internal guard sources SkillLibrary never touches.
    Read order is the scoping priority used to break selection ties:
    installed skills (project, then personal, then bundled precedence),
    then learned guards.

    Returns empty list on any failure — never propagates exceptions.
    """
    try:
        installed, diagnostics = SkillLibrary(workspace_root).discover_effective_skills()
        _report_discovery_diagnostics(diagnostics)
        graduated = _read_graduated_skills(workspace_root, window_days=window_days)
        refined = _read_refined_skills(workspace_root)
        return installed + graduated + refined
    except Exception:
        logger.debug("read_skills failed", exc_info=True)
        return []
