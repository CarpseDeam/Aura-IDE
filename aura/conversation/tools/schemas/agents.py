"""The root-only Agent delegation and workflow tool schemas.

``delegate_agent`` is the one tool through which a parent hands a bounded piece
of work to one of the user's own agents. Aura's root variant exists only with a
frozen turn roster. Each workflow Agent with dashed children instead receives a
helper-specific variant containing only its frozen immediate children. Ordinary
children and helper leaves receive neither variant.

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


def _workflow_helper_lines(rows: Iterable[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        node_id = str(row.get("helper_node_id") or "").strip()
        if not node_id:
            continue
        name = str(row.get("name") or row.get("agent_name") or "").strip()
        name = name or node_id
        permission = str(row.get("permission_label") or "Read only").strip()
        description = " ".join(str(row.get("description") or "").split())
        assignment = " ".join(str(row.get("assignment") or "").split())
        details = "; ".join(
            part
            for part in (
                f"role here: {assignment}" if assignment else "",
                description,
            )
            if part
        )
        head = f"- {node_id} — {name} [{permission}]"
        lines.append(f"{head}: {details}" if details else head)
    return "\n".join(lines)


def build_workflow_helper_tool_def(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one workflow Agent's direct-child ``delegate_agent`` variant.

    Helper occurrences are addressed by node id, never reusable Agent id, so
    two dashed occurrences of the same Agent remain distinct. Unlike root
    delegation, this schema promises no new worktree or checkpoint: helpers
    use the workflow's already-selected effective workspace synchronously.
    """
    frozen = tuple(rows)
    node_ids = [
        str(row.get("helper_node_id") or "").strip()
        for row in frozen
        if str(row.get("helper_node_id") or "").strip()
    ]
    helpers = _workflow_helper_lines(frozen)
    return {
        "type": "function",
        "function": {
            "name": "delegate_agent",
            "description": (
                "Ask one optional helper attached directly to this workflow Agent "
                "to do one "
                "bounded piece of work, wait for it, and receive one structured "
                "result in this Agent's private history. The helper reads the same "
                "effective workspace as the workflow. A Read / Write helper uses "
                "its own frozen grant in the workflow's existing shared worktree; "
                "it does not create or checkpoint another worktree, and its grant "
                "does not widen your tools. A helper failure is a result for you to "
                "handle. That helper may invoke only helpers attached directly to "
                "it and listed in its own tool; it cannot run workflows or invoke "
                "root-roster Agents, siblings, ancestors, or unrelated descendants. "
                "You may call a directly attached helper more than once when useful, "
                "but helpers never run automatically. Aura owns the final response."
                "\n\nHelpers attached directly to this Agent:\n"
                + (helpers or "- (none)")
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "helper_node_id": {
                        "type": "string",
                        "enum": node_ids,
                        "description": (
                            "The helper occurrence node id exactly as listed above. "
                            "Use the node id even when multiple occurrences reuse "
                            "the same Agent."
                        ),
                    },
                    "task": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_DELEGATED_TASK_CHARS,
                        "description": (
                            "The bounded task this helper should perform for your "
                            "work. It also receives the original workflow task and "
                            "its frozen occurrence assignment."
                        ),
                    },
                },
                "required": ["helper_node_id", "task"],
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
        after = [str(item).strip() for item in (step.get("after") or ()) if str(item).strip()]
        head = f"{position}. {name} [{permission}]"
        if after:
            head += f" (after {', '.join(after)})"
        lines.append(f"{head}: {assignment}" if assignment else head)
        pending = [
            (helper, 1)
            for helper in reversed(tuple(step.get("helpers") or ()))
        ]
        while pending:
            helper, depth = pending.pop()
            helper_node_id = str(helper.get("helper_node_id") or "").strip()
            helper_name = str(
                helper.get("agent_name") or helper.get("name") or "a helper"
            ).strip()
            helper_permission = str(
                helper.get("permission_label") or "Read only"
            ).strip()
            helper_assignment = " ".join(
                str(helper.get("assignment") or "").split()
            )
            helper_head = (
                f"{'   ' * depth}Optional helper {helper_node_id} — {helper_name} "
                f"[{helper_permission}]"
            )
            lines.append(
                f"{helper_head}: {helper_assignment}"
                if helper_assignment
                else helper_head
            )
            pending.extend(
                (child, depth + 1)
                for child in reversed(tuple(helper.get("helpers") or ()))
            )
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
    step_rows = tuple(workflow.get("steps") or ())
    steps = _workflow_step_lines(step_rows)
    has_helpers = any(step.get("helpers") for step in step_rows)
    helper_copy = (
        "Each workflow Agent may optionally call only the helpers shown directly "
        "beneath it; helpers never run automatically and return only to their "
        "immediate caller. "
        if has_helpers
        else ""
    )
    branched = bool(workflow.get("branched"))
    writable = bool(workflow.get("writable"))
    if writable and has_helpers:
        authority = (
            "At least one solid Step or helper descendant may edit files. Every "
            "child in this run reads one shared isolated Git worktree, and the "
            "whole run is checkpointed into a single change set. A Step that may "
            "edit, or whose helper tree contains Read / Write authority, runs "
            "exclusively. Nothing is written to the user's workspace unless the "
            "user later approves applying it."
        )
    elif writable:
        authority = (
            "At least one step may edit files. Every step of this run shares one "
            "isolated Git worktree, and the whole run is checkpointed into a "
            "single change set. A Step that may edit runs exclusively. Nothing is "
            "written to the user's workspace unless they later approve applying it."
        )
    elif has_helpers:
        authority = "Every solid Step and helper descendant is read-only."
    else:
        authority = "Every step of this workflow is read-only."
    order_copy = (
        "DAG edges are the ordering contract. Independent ready read-only Steps "
        "may overlap; every Step that may edit or has a Read / Write descendant runs "
        "exclusively. A Step that must consume another Step's edits must be its "
        "successor. A branch that fans out runs every path, and a Step marked "
        "(after ...) waits for every named predecessor to settle successfully. "
        "Joins therefore wait for every predecessor. Results are always returned "
        "in the frozen workflow order, never completion order."
        if branched
        else (
            "The agents below run one after another according to their DAG edges. "
            "A mutation-capable Step runs exclusively, and results retain this "
            "frozen order."
        )
    )
    return {
        "type": "function",
        "function": {
            "name": "run_agent_workflow",
            "description": (
                f"Run the user's Agent workflow '{name}' and wait for its "
                "result. " + order_copy + " Each is given your task, its own "
                "assignment, and the structured result of the step or steps before "
                "it, and none of them can see this conversation. "
                f"{helper_copy}{authority} You receive one structured result"
                + (
                    ", carrying every branch that reaches the end in the "
                    "workflow's own order,"
                    if branched
                    else ""
                )
                + " and write the final answer to the user yourself.\n\n"
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
    "build_workflow_helper_tool_def",
]
