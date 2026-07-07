"""Worker Context Pack — deterministic, read-only context substrate for Workers."""

from __future__ import annotations

from pathlib import Path

from aura.conversation.context_pack.worker_pack import assemble_worker_context_pack
from aura.work_artifact.model import ValidationCommandSpec

__all__ = ["build_worker_context_pack"]


def build_worker_context_pack(
    workspace_root: Path | None,
    *,
    files: list[str],
    goal: str,
    spec: str,
    acceptance: str,
    validation_commands: list[ValidationCommandSpec] | None = None,
    max_chars: int = 12000,
) -> str:
    """Build a Worker Context Pack string.

    Returns an empty string when *workspace_root* is ``None`` or *files* is
    empty.  Otherwise delegates to ``assemble_worker_context_pack``.
    """
    if workspace_root is None:
        return ""
    if not files:
        return ""
    return assemble_worker_context_pack(
        workspace_root,
        files=files,
        goal=goal,
        spec=spec,
        acceptance=acceptance,
        validation_commands=validation_commands,
        max_chars=max_chars,
    )
