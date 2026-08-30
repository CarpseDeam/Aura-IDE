"""The root-only agent delegation tool schema.

``delegate_agent`` is the one tool through which Aura hands a bounded piece of
work to one of the user's own agents.  It exists in a request only when that
turn's frozen roster is non-empty, and it is never present in a child agent's
catalog — that is what makes delegation one level deep.

The schema is built per turn from the frozen roster so the model is told
exactly which agents it may address.  What it is told about each is the whole
of what it may know: the immutable id, the display name, and the short
description the agent's author wrote for this purpose.  An agent's full
instructions are the child's brief and never appear here.
"""
from __future__ import annotations

from typing import Any, Iterable

#: Upper bound on the parent-authored task, in characters.  A delegated task
#: is a brief, not a transcript: the child has the workspace and its own read
#: tools, so what it needs from Aura is the ask, not the context dump.
MAX_DELEGATED_TASK_CHARS = 4000


def _roster_lines(rows: Iterable[dict[str, str]]) -> str:
    lines: list[str] = []
    for row in rows:
        agent_id = str(row.get("agent_id") or "").strip()
        if not agent_id:
            continue
        name = str(row.get("name") or "").strip() or agent_id
        description = str(row.get("description") or "").strip()
        permission = str(row.get("permission_label") or "Read only").strip()
        lines.append(f"- {agent_id} — {name} [{permission}]: {description}" if description
                     else f"- {agent_id} — {name} [{permission}]")
    return "\n".join(lines)


def build_delegate_agent_tool_def(
    rows: Iterable[dict[str, str]],
) -> dict[str, Any]:
    """Build ``delegate_agent`` for one turn's frozen roster.

    *rows* are the compact id/name/description rows from
    :meth:`~aura.agents.roster.AgentTurnRoster.catalog_rows`, in the user's
    own order.
    """
    roster = _roster_lines(rows)
    return {
        "type": "function",
        "function": {
            "name": "delegate_agent",
            "description": (
                "Hand one bounded piece of work to one of the user's agents and "
                "wait for its result. Each listed agent carries the user's frozen "
                "effective grant for this invocation: read only; edit in an "
                "isolated Git worktree; or isolated edit plus terminal. Provider "
                "and model selection cannot widen that grant. Writable results "
                "are checkpointed on an Aura-owned branch and never land in the "
                "canonical workspace automatically. The agent starts with no "
                "knowledge of this conversation, so "
                "the task you write is everything it will know. It runs to "
                "completion before anything else in this turn continues, and it "
                "returns a structured status and final report.\n\n"
                "Available agents:\n" + (roster or "- (none)")
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "The id of the agent to delegate to, exactly as "
                            "listed above. Only those ids are available."
                        ),
                    },
                    "task": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_DELEGATED_TASK_CHARS,
                        "description": (
                            "The complete, self-contained task for the agent, in "
                            "your own words. State what to investigate, which "
                            "files or areas are relevant, and what the answer "
                            "should contain. The agent cannot see this "
                            "conversation and cannot ask a follow-up question."
                        ),
                    },
                },
                "required": ["agent_id", "task"],
                "additionalProperties": False,
            },
        },
    }


__all__ = [
    "MAX_DELEGATED_TASK_CHARS",
    "build_delegate_agent_tool_def",
]
