"""Terminal-driven syntax repair state mutation extracted from ConversationManager."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.conversation.path_utils import (
    is_validation_scratch_path,
    normalize_worker_path,
)
from aura.conversation.syntax_repair_state import (
    discard_syntax_validation_path,
    pop_syntax_repair_state,
    syntax_repair_state_for_path,
)
from aura.conversation.terminal_syntax import (
    py_compile_targets,
)


def update_syntax_state_from_terminal(
    *,
    args: dict[str, Any],
    loop_info: dict[str, Any] | None,
    workspace_root: Path,
    syntax_repair_required: dict[str, dict[str, Any]],
    syntax_validation_required: set[str],
    stale_validation_notes: list[str] | None = None,
) -> None:
    payload = loop_info.get("_terminal_payload") if isinstance(loop_info, dict) else None
    if not isinstance(payload, dict):
        return
    command = str(payload.get("command") or args.get("command") or "")
    targets = [
        normalize_worker_path(path)
        for path in py_compile_targets(command)
        if not is_validation_scratch_path(path)
        and not (
            "/" not in path
            and not (workspace_root / path).exists()
        )
    ]
    if not targets:
        return
    if payload.get("exit_code") == 0:
        for path in targets:
            state = syntax_repair_state_for_path(syntax_repair_required, path)
            if state and state.get("awaiting_validation") is False:
                if stale_validation_notes is not None:
                    stale_validation_notes.append(
                        "Stale validation cleared: "
                        f"py_compile passed for {path} after a prior craft-gate rejection."
                    )
            pop_syntax_repair_state(syntax_repair_required, path)
            discard_syntax_validation_path(syntax_validation_required, path)
        return

