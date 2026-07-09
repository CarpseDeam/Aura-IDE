from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from aura.conversation.project_profile import detect_project_profile
from aura.projects.store import ProjectStore

logger = logging.getLogger(__name__)


def gather_workspace_snapshot(workspace_root: Path) -> dict[str, Any]:
    root = workspace_root.resolve()
    result: dict[str, Any] = {
        "workspace_root": str(root),
        "project": None,
        "threads": [],
        "git": None,
        "project_hints": [],
    }

    # Project identity
    store = ProjectStore()
    project = store._load_project_from_root(root)
    if project is not None:
        result["project"] = {
            "name": project.name,
            "id": project.id,
            "updated_at": project.updated_at,
        }
        try:
            threads = store.list_threads(project, include_archived=False)
            for t in threads[:10]:
                result["threads"].append(
                    {
                        "id": t.id,
                        "title": t.title,
                        "updated_at": t.updated_at,
                    }
                )
        except Exception:
            logger.warning("Failed to list threads in snapshot")

    # Git state
    try:
        git_dir = root / ".git"
        if git_dir.is_dir():
            branch = subprocess.run(
                ["git", "-C", str(root), "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            status_out = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            changed_lines = [line for line in status_out.splitlines() if line.strip()]
            rev = subprocess.run(
                ["git", "-C", str(root), "rev-list", "--left-right", "--count", "HEAD...@{u}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ahead, behind = 0, 0
            if rev.returncode == 0:
                parts = rev.stdout.strip().split()
                if len(parts) == 2:
                    behind, ahead = int(parts[0]), int(parts[1])
            result["git"] = {
                "branch": branch,
                "is_git_repo": True,
                "has_uncommitted_changes": len(changed_lines) > 0,
                "changed_files_count": len(changed_lines),
                "ahead": ahead,
                "behind": behind,
            }
    except Exception:
        logger.warning("Failed to gather git state in snapshot")
        result["git"] = {"is_git_repo": False, "branch": None}

    # Project hints
    hints_map = {
        "pyproject.toml": "Python (pyproject.toml)",
        "package.json": "Node/JavaScript (package.json)",
        "Cargo.toml": "Rust (Cargo.toml)",
        "go.mod": "Go (go.mod)",
        "Gemfile": "Ruby (Gemfile)",
        "composer.json": "PHP (composer.json)",
        "CMakeLists.txt": "C/C++ (CMakeLists.txt)",
        "setup.py": "Python",
        "requirements.txt": "Python",
        "Pipfile": "Python",
        "Makefile": "Makefile",
    }
    for filename, label in hints_map.items():
        if (root / filename).is_file():
            result["project_hints"].append(label)

    # Project profile
    try:
        profile = detect_project_profile(root)
        result["project_profile"] = {
            "workspace_root": profile.workspace_root,
            "project_types": list(profile.project_types),
            "manifests": list(profile.manifests),
            "lockfiles": list(profile.lockfiles),
            "package_manager": profile.package_manager,
            "has_venv": profile.has_venv,
            "python_venv_path": profile.python_venv_path,
            "python_executable": profile.python_executable,
            "godot_project_root": profile.godot_project_root,
            "godot_executable": profile.godot_executable,
            "godot_setup_message": profile.godot_setup_message,
            "declared_dependencies": list(profile.declared_dependencies),
            "validation_commands": list(profile.validation_commands),
            "node_scripts": [list(s) for s in profile.node_scripts],
            "summary": profile.summarize(),
        }
    except Exception:
        logger.warning("Failed to detect project profile", exc_info=True)

    return result
