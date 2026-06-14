from __future__ import annotations

from pathlib import Path


def workspaces_dir(project_root: Path) -> Path:
    """Return the root drone-workspaces directory under .aura."""
    return project_root / ".aura" / "drone-workspaces"


def workspace_folder(project_root: Path, workspace_id: str) -> Path:
    """Return the workspace directory for a given workspace id."""
    return workspaces_dir(project_root) / workspace_id


def workspace_manifest_path(project_root: Path, workspace_id: str) -> Path:
    """Return the workspace.json path for a given workspace id."""
    return workspace_folder(project_root, workspace_id) / "workspace.json"


def active_workspace_path(project_root: Path) -> Path:
    """Return the _active.json pointer path under drone-workspaces."""
    return workspaces_dir(project_root) / "_active.json"


def chats_dir(project_root: Path, workspace_id: str) -> Path:
    return workspace_folder(project_root, workspace_id) / "chats"


def candidate_dir(project_root: Path, workspace_id: str) -> Path:
    return workspace_folder(project_root, workspace_id) / "candidate"


def build_runs_dir(project_root: Path, workspace_id: str) -> Path:
    return workspace_folder(project_root, workspace_id) / "build-runs"


def proof_runs_dir(project_root: Path, workspace_id: str) -> Path:
    return workspace_folder(project_root, workspace_id) / "proof-runs"


def repair_runs_dir(project_root: Path, workspace_id: str) -> Path:
    return workspace_folder(project_root, workspace_id) / "repair-runs"


def artifacts_dir(project_root: Path, workspace_id: str) -> Path:
    return workspace_folder(project_root, workspace_id) / "artifacts"
