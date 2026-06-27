"""Deterministic validation selection layer.

Picks the right validation plan based on task shape, target files,
changed files, and loaded Context Gearbox packs.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

ValidationPlan = dict[str, Any]


def select_validation_plan(
    target_files: list[str],
    changed_files: list[str] | None = None,
    task_kind: str = "unknown",
    context_gearbox: dict[str, Any] | None = None,
    workspace_root: Path | None = None,
    existing_validation_text: str | None = None,
) -> ValidationPlan:
    """Return a deterministic validation plan based on files and context.

    Parameters
    ----------
    target_files : list[str]
        Files the task is scoped to.
    changed_files : list[str] | None
        Files actually modified during execution.
    task_kind : str
        The inferred task shape kind (e.g. ``"gui_polish"``).
    context_gearbox : dict[str, Any] | None
        Metadata from the context gearbox, including loaded source IDs.
    workspace_root : Path | None
        Root of the workspace (unused in selection, available for future use).
    existing_validation_text : str | None
        Pre-existing validation text from a prior run (unused in selection).

    Returns
    -------
    ValidationPlan
        A plain-data dict with keys ``kind``, ``commands``, ``reason``,
        ``confidence``, and ``skipped``.
    """
    # Normalise all paths to forward-slash for cross-platform glob matching.
    all_candidates: list[str] = []
    if target_files:
        all_candidates.extend(p.replace("\\", "/") for p in target_files)
    if changed_files:
        all_candidates.extend(p.replace("\\", "/") for p in changed_files)

    # Extract loaded context-gearbox source IDs.
    loaded_sources: list[str] = _loaded_source_ids(context_gearbox)

    # ── Ordered selection rules ───────────────────────────────────────

    # 1. GUI validation
    if _any_matches(all_candidates, _GUI_PATTERNS) or "gui_rules" in loaded_sources:
        return _plan(
            kind="gui",
            commands=["python -m compileall aura/gui", "python -m aura --selfcheck"],
            reason="GUI files changed",
            confidence="focused",
        )

    # 2. Drone validation
    if _any_matches(all_candidates, _DRONE_PATTERNS) or "drone_rules" in loaded_sources:
        return _plan(
            kind="drone",
            commands=["python -m compileall aura/drones", "python -m aura --selfcheck"],
            reason="Drone files changed",
            confidence="focused",
        )

    # 3. Provider validation
    if _any_matches(all_candidates, _PROVIDER_PATTERNS) or "provider_rules" in loaded_sources:
        return _plan(
            kind="provider",
            commands=[
                "python -m compileall aura/providers aura/backends aura/client",
                "python -m aura --selfcheck",
            ],
            reason="Provider/backend/client files changed",
            confidence="focused",
        )

    # 4. Build validation
    if _any_matches(all_candidates, _BUILD_PATTERNS) or "build_pipeline_rules" in loaded_sources:
        return _plan(
            kind="build",
            commands=["python -m compileall scripts/", "python -m aura --selfcheck"],
            reason="Build/packaging files changed",
            confidence="focused",
            skipped=["packaging build skipped \u2014 use --package explicitly to run full build"],
        )

    # 5. General Python validation
    python_dirs = _collect_python_dirs(all_candidates)
    if python_dirs:
        compile_command = "python -m compileall " + " ".join(sorted(python_dirs))
        return _plan(
            kind="general_python",
            commands=[compile_command, "python -m aura --selfcheck"],
            reason="General Python files changed",
            confidence="general",
        )

    # 6. Not applicable
    return _plan(
        kind="not_applicable",
        commands=[],
        reason="No Python files changed",
        confidence="skipped",
        skipped=["validation not applicable \u2014 no Python files changed"],
    )


# ── Internal helpers ──────────────────────────────────────────────────


def _loaded_source_ids(context_gearbox: dict[str, Any] | None) -> list[str]:
    """Extract loaded source IDs from the context gearbox metadata."""
    if not isinstance(context_gearbox, dict):
        return []
    summary = context_gearbox.get("summary", {})
    if not isinstance(summary, dict):
        return []
    loaded = summary.get("loaded", [])
    if isinstance(loaded, list):
        return [str(item) for item in loaded if item]
    return []


def _any_matches(candidates: list[str], patterns: list[str]) -> bool:
    """Return True if any candidate matches any of the given fnmatch patterns."""
    for path in candidates:
        for pattern in patterns:
            if fnmatch.fnmatchcase(path, pattern):
                return True
    return False


def _collect_python_dirs(candidates: list[str]) -> list[str]:
    """Collect unique parent directories of `.py` files from candidates."""
    dirs: set[str] = set()
    for path in candidates:
        p = path.replace("\\", "/")
        if p.endswith(".py"):
            parent = p.rsplit("/", 1)[0] if "/" in p else p
            dirs.add(parent)
    return list(dirs)


def _plan(
    kind: str,
    commands: list[str],
    reason: str,
    confidence: str,
    skipped: list[str] | None = None,
) -> ValidationPlan:
    """Build and return a deterministic validation plan."""
    # Deduplicate commands while preserving first-seen order.
    deduped = list(dict.fromkeys(commands))
    return {
        "kind": kind,
        "commands": deduped,
        "reason": reason,
        "confidence": confidence,
        "skipped": skipped or [],
    }


# ── Pattern lists (static, order-sensitive) ──────────────────────────

_GUI_PATTERNS: list[str] = [
    "aura/gui/*",
    "aura/gui/**/*",
    "aura/assets/*",
    "aura/assets/**/*",
    "media/ui/**",
    "media/ui_assets/**",
    "media/**/ui/**",
    "media/**/*ui*",
]

_DRONE_PATTERNS: list[str] = [
    "aura/drones/*",
    "aura/drones/**/*",
    "aura/gui/drone*",
    "drones/*",
    "bundled_drones/*",
    "**/drone_manifest*.json",
    "**/drone_manifests/**",
    "**/drone_templates/**",
]

_PROVIDER_PATTERNS: list[str] = [
    "aura/providers/*",
    "aura/providers/**/*",
    "aura/backends/*",
    "aura/backends/**/*",
    "aura/client/*",
    "aura/client/**/*",
    "aura/**/*provider*settings*.py",
    "aura/*provider*settings*.py",
    "aura/**/*settings*provider*.py",
    "aura/*settings*provider*.py",
]

_BUILD_PATTERNS: list[str] = [
    "scripts/build_*.py",
    "installer/*",
    "installer/**/*",
    "packaging/*",
    "packaging/**/*",
    "pyproject.toml",
    "requirements*.txt",
    "**/nuitka/**",
    "nuitka/*",
    "**/installer/**",
    "**/packaging/**",
]
