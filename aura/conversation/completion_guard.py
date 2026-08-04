"""Recognize when an action/task is completed and whether the model is producing
repetitive completion messages.
"""
from __future__ import annotations

import re
from typing import Any

from aura.conversation.tool_limits import WRITE_TOOLS

COMPLETION_PHRASE_MARKERS = (
    "all set",
    "staged and ready",
    "ready for you",
    "let me know",
    "if you need anything else",
    "committed and done",
    "everything else is in good shape",
    "when you want to commit",
    "no further action needed",
)

TASK_COMPLETION_TOOL_NAMES = {
    "run_and_watch",
    "run_terminal_command",
    "run_diagnostic_command",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_log_file",
}

ACTION_COMPLETION_TOOL_NAMES = TASK_COMPLETION_TOOL_NAMES | WRITE_TOOLS


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""



def terminal_result_completed(
    info: dict[str, Any] | None, *, probes_complete_action: bool = True
) -> bool:
    """Whether a terminal round finished the turn's action.

    A terminal command is a probe, so ``probes_complete_action=False`` answers
    ``False`` however cleanly it exited.  See
    :func:`tool_result_completes_action` for why that distinction exists.
    """
    if not probes_complete_action:
        return False
    payload = info.get("_terminal_payload") if isinstance(info, dict) else None
    return isinstance(payload, dict) and payload.get("exit_code") == 0


def tool_result_completes_action(
    name: str, ok: bool, *, probes_complete_action: bool = True
) -> bool:
    """Whether this tool result means the turn completed its action.

    Two different things live in :data:`ACTION_COMPLETION_TOOL_NAMES`:

    * a **write** — the action itself.  A successful one always completes it.
    * a **probe** — ``git_status``, ``git_diff``, ``run_diagnostic_command``,
      the terminal tools.  These inspect; they do not act.

    Treating a successful probe as the completed action is right on a turn whose
    point *is* the probe ("show me the git status", "run the tests").  It is
    false on an implementation turn that has not yet written anything: the action
    is the edit, and no edit has happened.

    That distinction is load-bearing rather than pedantic.
    ``task_completion_context`` vetoes the focused action transition, so before
    this parameter existed a single successful ``git_status`` early in an
    implementation turn convinced the loop the turn had already acted — and the
    model could then keep making novel reads indefinitely, with the transition
    that would have ended the turn permanently suppressed.

    Callers that own the answer pass ``probes_complete_action=False``; the
    default preserves the historical behaviour for every other caller.
    """
    if not ok:
        return False
    if name in WRITE_TOOLS:
        return True
    return probes_complete_action and name in TASK_COMPLETION_TOOL_NAMES


def completion_phrase_hits(text: str) -> set[str]:
    lowered = " ".join(str(text or "").lower().split())
    return {
        marker
        for marker in COMPLETION_PHRASE_MARKERS
        if marker in lowered
    }


def is_completion_style_message(text: str) -> bool:
    return bool(completion_phrase_hits(text))


def is_repetitive_completion_final(current: str, previous: str) -> bool:
    current_hits = completion_phrase_hits(current)
    previous_hits = completion_phrase_hits(previous)
    if current_hits and (current_hits & previous_hits):
        return True
    return text_overlap_ratio(current, previous) >= 0.7


def text_overlap_ratio(left: str, right: str) -> float:
    left_words = set(re.findall(r"[a-z0-9_]+", str(left).lower()))
    right_words = set(re.findall(r"[a-z0-9_]+", str(right).lower()))
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / max(len(left_words), len(right_words))
