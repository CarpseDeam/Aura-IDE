"""Deterministic validation selection layer.

Picks the right validation plan based on task shape, target files,
changed files, and loaded Context Gearbox packs.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from aura.godot_toolchain import (
    build_godot_check_command,
    find_godot_project_root,
    resolve_godot_executable,
)

ValidationPlan = dict[str, Any]

_KIND_LABELS: dict[str, str] = {
    "gui": "GUI",
    "drone": "drone",
    "provider": "provider",
    "build": "build",
    "general_python": "general Python",
    "not_applicable": "skipped",
    "godot": "Godot",
}

# Known source file to test file mappings (non-exhaustive, covers common
# targets).  Used by _suggest_focused_tests to produce suggestions even
# without a workspace_root.
_KNOWN_TEST_MAPPINGS: dict[str, str] = {
    "aura/gui/info_hub_pane.py": "tests/test_info_hub_pane.py",
    "aura/validation/selector.py": "tests/test_validation_selector.py",
    "aura/gui/worker_handler.py": "tests/test_worker_handler.py",
    "aura/bridge/dispatch.py": "tests/test_dispatch.py",
    "aura/context_gearbox/sources.py": "tests/test_context_gearbox.py",
}


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
        Root of the workspace, used to verify test file existence.
    existing_validation_text : str | None
        Pre-existing validation text from a prior run (unused in selection).

    Returns
    -------
    ValidationPlan
        A plain-data dict with keys ``kind``, ``commands``, ``reason``,
        ``confidence``, ``skipped``, ``display``, and optionally
        ``test_suggestions_skipped``.
    """
    # Normalise all paths to forward-slash for cross-platform glob matching.
    all_candidates: list[str] = []
    if target_files:
        all_candidates.extend(p.replace("\\", "/") for p in target_files)
    if changed_files:
        all_candidates.extend(p.replace("\\", "/") for p in changed_files)

    # Extract changed .py files for focused compile commands.
    changed_py_files: list[str] = []
    changed_gd_files: list[str] = []
    if changed_files:
        changed_py_files = [
            p.replace("\\", "/") for p in changed_files
            if p.replace("\\", "/").endswith(".py")
        ]
        changed_gd_files = [
            p.replace("\\", "/") for p in changed_files
            if p.replace("\\", "/").lower().endswith(".gd")
        ]

    # Extract loaded context-gearbox source IDs.
    loaded_sources: list[str] = _loaded_source_ids(context_gearbox)

    # Compute test suggestions once used by several lanes below.
    _test_cmds, _test_skipped = _suggest_focused_tests(all_candidates, workspace_root)

    # ── Ordered selection rules ───────────────────────────────────────

    # 1. Godot validation uses the real editor binary for touched GDScript.
    if changed_gd_files and workspace_root is not None:
        workspace = Path(workspace_root).resolve(strict=False)
        godot_root = find_godot_project_root(workspace)
        if godot_root is not None:
            resolution = resolve_godot_executable(godot_root)
            if not resolution.available:
                plan = _plan(
                    kind="godot",
                    commands=[],
                    reason="GDScript files changed but Godot is unavailable",
                    confidence="unavailable",
                    skipped=[resolution.message],
                )
                plan["available"] = False
                plan["setup_message"] = resolution.message
                return plan

            commands: list[str] = []
            for changed_path in sorted(changed_gd_files):
                file_path = Path(changed_path)
                if not file_path.is_absolute():
                    file_path = workspace / file_path
                try:
                    command = build_godot_check_command(
                        resolution.path,
                        godot_root,
                        file_path,
                    )
                except ValueError:
                    continue
                if command:
                    commands.append(command)
            plan = _plan(
                kind="godot",
                commands=commands,
                reason="GDScript files changed in a Godot project",
                confidence="focused",
            )
            plan["available"] = True
            plan["godot_executable"] = str(resolution.path)
            plan["project_root"] = str(godot_root)
            return plan

    # 2. GUI validation
    if _any_matches(all_candidates, _GUI_PATTERNS) or "gui_rules" in loaded_sources:
        _c = _focused_python_commands(changed_py_files, "python -m compileall aura/gui")
        return _plan(
            kind="gui",
            commands=_c + _test_cmds + ["python -m aura --selfcheck"],
            reason="GUI files changed",
            confidence="focused",
            test_suggestions_skipped=_test_skipped,
        )

    # 3. Drone validation
    if _any_matches(all_candidates, _DRONE_PATTERNS) or "drone_rules" in loaded_sources:
        _c = _focused_python_commands(changed_py_files, "python -m compileall aura/drones")
        return _plan(
            kind="drone",
            commands=_c + _test_cmds + ["python -m aura --selfcheck"],
            reason="Drone files changed",
            confidence="focused",
            test_suggestions_skipped=_test_skipped,
        )

    # 4. Provider validation
    if _any_matches(all_candidates, _PROVIDER_PATTERNS) or "provider_rules" in loaded_sources:
        _c = _focused_python_commands(changed_py_files, "python -m compileall aura/providers aura/backends aura/client")
        return _plan(
            kind="provider",
            commands=_c + _test_cmds + ["python -m aura --selfcheck"],
            reason="Provider/backend/client files changed",
            confidence="focused",
            test_suggestions_skipped=_test_skipped,
        )

    # 5. Build validation
    if _any_matches(all_candidates, _BUILD_PATTERNS) or "build_pipeline_rules" in loaded_sources:
        _c = _focused_python_commands(changed_py_files, "python -m compileall scripts/")
        return _plan(
            kind="build",
            commands=_c + _test_cmds + ["python -m aura --selfcheck"],
            reason="Build/packaging files changed",
            confidence="focused",
            skipped=["packaging build skipped \u2014 use --package explicitly to run full build"],
            test_suggestions_skipped=_test_skipped,
        )

    # 6. General Python validation
    python_dirs = _collect_python_dirs(all_candidates)
    if python_dirs:
        compile_command = "python -m compileall " + " ".join(sorted(python_dirs))
        python_commands = (
            _focused_python_commands(changed_py_files, compile_command)
            if changed_py_files
            else [compile_command]
        )
        return _plan(
            kind="general_python",
            commands=python_commands + _test_cmds + ["python -m aura --selfcheck"],
            reason="General Python files changed",
            confidence="general",
            test_suggestions_skipped=_test_skipped,
        )

    # 7. Not applicable
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


def _focused_compile_commands(changed_py_files: list[str], fallback_compile_cmd: str) -> list[str]:
    """Return focused ``py_compile`` commands when .py files were changed.

    When ``changed_py_files`` is non-empty, produce a single
    ``python -m py_compile`` command listing every changed .py file.
    Otherwise fall back to the broad ``compileall`` command.
    """
    if changed_py_files:
        return ["python -m py_compile " + " ".join(sorted(changed_py_files))]
    return [fallback_compile_cmd]


def _focused_ruff_commands(changed_py_files: list[str]) -> list[str]:
    """Return a focused Ruff check for changed Python files only."""
    focused = sorted(
        p.replace("\\", "/")
        for p in changed_py_files
        if p.replace("\\", "/").endswith(".py")
    )
    if not focused:
        return []
    return ["python -m ruff check " + " ".join(focused)]


def _focused_python_commands(
    changed_py_files: list[str],
    fallback_compile_cmd: str,
) -> list[str]:
    """Return focused Python validation when files are known, else fallback."""
    return _focused_compile_commands(
        changed_py_files,
        fallback_compile_cmd,
    ) + _focused_ruff_commands(changed_py_files)


def _suggest_focused_tests(
    files: list[str],
    workspace_root: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Return focused pytest command suggestions for the given files.

    Attempts to map each .py file path to a likely test file using:

    1.  Known project-specific mappings (see ``_KNOWN_TEST_MAPPINGS``).
    2.  A general stem-based rule:
        ``aura/gui/{stem}.py`` → ``tests/test_{stem}.py``.

    Suggestions are only included when the test file can be verified to
    exist on disk via ``workspace_root``, or the mapping is a known one.

    Returns
    -------
    (suggestions, test_suggestions_skipped)
        ``suggestions`` is at most 3 pytest command strings.
        ``test_suggestions_skipped`` records candidate test paths that
        could not be verified.
    """
    if not files:
        return [], []

    suggestions: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for file_path in files:
        p = file_path.replace("\\", "/")
        if not p.endswith(".py"):
            continue

        candidates = _candidate_test_paths(p)
        for test_path in candidates:
            if test_path in seen:
                continue
            seen.add(test_path)

            # Known mappings are always suggested.
            if p in _KNOWN_TEST_MAPPINGS and _KNOWN_TEST_MAPPINGS[p] == test_path:
                suggestions.append(f"python -m pytest {test_path} -q")
                continue

            # General rule: verify existence.
            if workspace_root is not None:
                if (workspace_root / test_path).is_file():
                    suggestions.append(f"python -m pytest {test_path} -q")
                else:
                    skipped.append(f"test file not found: {test_path}")
            # When workspace_root is None, skip general-rule suggestions
            # (cannot verify existence).
            else:
                skipped.append(f"could not verify (no workspace_root): {test_path}")

    return suggestions[:3], skipped


def _candidate_test_paths(file_path: str) -> list[str]:
    """Generate candidate test file paths for a given source file.

    Order matters — the first match is preferred.
    """
    # 1. Known specific mapping
    if file_path in _KNOWN_TEST_MAPPINGS:
        return [_KNOWN_TEST_MAPPINGS[file_path]]

    # 2. Strip subdirectory prefix like /cards/ and retry.
    #    e.g. aura/gui/cards/foo.py → aura/gui/foo.py
    for subdir in ("/cards/", "/widgets/", "/panels/"):
        if subdir in file_path:
            parts = file_path.split(subdir, 1)
            if len(parts) > 1:
                candidate = parts[0] + "/" + parts[1]
                if candidate in _KNOWN_TEST_MAPPINGS:
                    return [_KNOWN_TEST_MAPPINGS[candidate]]

    # 3. General stem rule
    stem = file_path.rsplit("/", 1)[-1][:-3]  # e.g. "worker_handler"
    return [f"tests/test_{stem}.py"]


def _plan(
    kind: str,
    commands: list[str],
    reason: str,
    confidence: str,
    skipped: list[str] | None = None,
    display: str | None = None,
    test_suggestions_skipped: list[str] | None = None,
) -> ValidationPlan:
    """Build and return a deterministic validation plan.

    Automatically computes the ``display`` field if not provided.
    """
    # Deduplicate commands while preserving first-seen order.
    deduped = list(dict.fromkeys(commands))
    if display is None:
        kind_label = _KIND_LABELS.get(kind, kind)
        n = len(deduped)
        if kind_label == confidence:
            display = f"Validation plan: {kind_label}, {n} checks selected"
        else:
            display = f"Validation plan: {kind_label} {confidence}, {n} checks selected"
    result: dict[str, Any] = {
        "kind": kind,
        "commands": deduped,
        "reason": reason,
        "confidence": confidence,
        "skipped": skipped or [],
        "display": display,
    }
    if test_suggestions_skipped:
        result["test_suggestions_skipped"] = test_suggestions_skipped
    return result


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
