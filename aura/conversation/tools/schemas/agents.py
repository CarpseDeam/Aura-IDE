"""The root-only Agent delegation and workflow tool schemas.

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

from typing import Any, Iterable, Mapping

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
                "effective grant for this invocation: Read only, which reads and "
                "nothing else; or Read / Write, which edits inside an isolated "
                "Git worktree and may run terminal commands there. A Read / "
                "Write agent never writes the user's workspace: its result is "
                "checkpointed on an Aura-owned branch and lands only if the user "
                "later approves applying it. Model selection cannot widen a "
                "grant. The agent starts with no knowledge of this conversation, "
                "so "
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

#: Upper bound on the task handed to a workflow. A workflow's own steps carry
#: their assignments, so what Aura writes here is the work itself, not a brief
#: for each agent in turn.
MAX_WORKFLOW_TASK_CHARS = 4000


def _workflow_step_lines(steps: Iterable[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for step in steps:
        position = step.get("position")
        name = str(step.get("agent_name") or "").strip() or "an agent"
        permission = str(step.get("permission_label") or "Read only").strip()
        assignment = " ".join(str(step.get("assignment") or "").split())
        head = f"{position}. {name} [{permission}]"
        lines.append(f"{head}: {assignment}" if assignment else head)
    return "\n".join(lines)


def build_run_workflow_tool_def(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """Build ``run_agent_workflow`` for the one workflow this turn froze.

    There is no workflow parameter. The user selected one workflow and
    switched it on, and that exact frozen plan is what runs — so the model
    cannot name a different one, and a workflow the user was merely looking
    at is not reachable by asking for it.
    """
    name = str(workflow.get("name") or "").strip() or "the selected workflow"
    description = str(workflow.get("description") or "").strip()
    steps = _workflow_step_lines(workflow.get("steps") or ())
    writable = bool(workflow.get("writable"))
    authority = (
        "At least one step may edit files. Every step of this run shares one "
        "isolated Git worktree, and the whole run is checkpointed into a "
        "single change set. Nothing is written to the user's workspace unless "
        "they later approve applying it."
        if writable
        else "Every step of this workflow is read-only."
    )
    return {
        "type": "function",
        "function": {
            "name": "run_agent_workflow",
            "description": (
                f"Run the user's Agent workflow '{name}' and wait for its "
                "result. The agents below run one after another, in this order; "
                "each is given your task, its own assignment, and the previous "
                "agent's structured result, and none of them can see this conversation. "
                f"{authority} You receive one structured result and write the "
                "final answer to the user yourself.\n\n"
                + (f"{description}\n\n" if description else "")
                + "Steps:\n"
                + (steps or "- (none)")
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_WORKFLOW_TASK_CHARS,
                        "description": (
                            "The complete, self-contained task for this "
                            "workflow, in your own words. Every step sees it, "
                            "so state what is wanted and what the final answer "
                            "should contain. The agents cannot see this "
                            "conversation and cannot ask a follow-up question."
                        ),
                    },
                },
                "required": ["task"],
                "additionalProperties": False,
            },
        },
    }


__all__ = [
    "MAX_DELEGATED_TASK_CHARS",
    "MAX_WORKFLOW_TASK_CHARS",
    "build_delegate_agent_tool_def",
    "build_run_workflow_tool_def",
]
