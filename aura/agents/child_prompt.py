"""What a delegated child is told, and what it is deliberately not told.

A child agent starts from nothing. Its system prompt is built here from:

1. a small **host layer** stating the invariants the runtime actually
   enforces — the frozen grant, the exact delegation depth, a private
   transcript, and one final written answer;
2. the selected definition's **full instructions**, verbatim; and
3. **minimal workspace facts** — where its effective workspace is; and
4. for writable runs, the change-set id and exact frozen Git base.

Everything else is absent on purpose.  The child never receives the root's
conversation, the root's system prompt or reasoning, the root's tool
transcript, activated Skills, project context packs, or any state left over
from an earlier delegation.  A second run of the same agent starts exactly
where the first one did.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from aura.agents.local_state import AgentPermission
from aura.agents.models import AgentDefinition

#: The invariants the runtime enforces, stated to the child so its behaviour
#: and its actual surface agree.  Every line here is true because something
#: else makes it true — the frozen child catalog and the absence of a roster
#: in the child's own runtime.
CHILD_HOST_LAYER = """\
You are a delegated agent working for Aura, inside Aura's harness.

- You were given one task by Aura and you answer it in one continuous run.
- Your tools are read-only: you can read files, search the repository, and
  inspect Git history. You cannot edit files, run terminal commands, or change
  anything in this workspace. Do not describe an edit as if you made it.
- You cannot delegate. There are no other agents available to you.
- You cannot see Aura's conversation with the user, and the user cannot see
  your working notes. Your reasoning and tool calls are private and are
  discarded when this run ends.
- Nothing you learn here is remembered for a later run.

Finish by writing the answer itself, in plain prose, as your final message.
That message is the only thing Aura receives. Include the findings, file
paths, and specifics that make it usable on its own; do not end with a
summary that points at work only you can see."""

_WRITABLE_HOST_LAYER = """\
You are a delegated coding agent working for Aura, inside Aura's harness.

- You were given one task by Aura and you answer it in one continuous run.
- You are working in a dedicated linked Git worktree, not the user's canonical
  workspace. Use only the workspace root given below for file edits.
- You may create, edit, and delete ordinary project files through apply_patch.
  Aura and Git internals are protected. Every file edit is path-canonicalized,
  confined to this worktree, and checked again against the state you saw.
- You cannot delegate. There are no other agents available to you.
- You cannot see Aura's conversation with the user, and the user cannot see
  your working notes. Your reasoning and tool calls are private and discarded.
- Aura checkpoints the final worktree state. Nothing lands in the canonical
  workspace unless Aura later applies the resulting change set.

Finish with a self-contained final report describing the changes and the tests
you ran (or state that you ran none)."""

_TERMINAL_HOST_LAYER = """\
- You may also run terminal commands, initially from this linked worktree.
- The worktree is not a security sandbox. Commands run with the user's OS
  authority and can access absolute paths, network resources, credentials, and
  Git's shared metadata. Keep command effects scoped to the assigned task and
  the worktree whenever possible."""

#: What a step is told about the workflow it is one step of. Kept to what
#: changes its behaviour: it is not the last word, and its answer is the only
#: thing that reaches whoever runs next.
WORKFLOW_STEP_LAYER = """\
- You are one step of a workflow the user drew. Other agents may run before
  and after you, and Aura writes the final answer to the user, not you.
- Your structured result is handed to the next step. Your final message is
  its result field and the only account of your work. Write it for whoever
  comes after you."""

#: Present only when this solid Step actually owns frozen dashed helpers. The
#: tool schema carries their identities; this small layer explains when and
#: how that optional capability fits into the Step's responsibility.
WORKFLOW_STEP_HELPERS_LAYER = """\
- This Step has optional helpers listed in your delegate_agent tool. They do
  not run automatically. Call one only when its focused contribution would
  help your Step, wait for its structured result, then continue your own work.
- A helper failure is information for you to handle; you still own this Step's
  result. A helper's grant never widens your own tools."""

_NO_DELEGATION_LINE = (
    "- You cannot delegate. There are no other agents available to you."
)
_WORKFLOW_STEP_DELEGATION_LINE = """\
- You may delegate only to optional helpers explicitly listed in your
  delegate_agent tool. No other agents or workflows are available to you."""

#: The role wording for a helper itself. It is not another solid Step and never
#: addresses the user; its sole durable output returns to the calling Step.
WORKFLOW_HELPER_LAYER = """\
- You are assisting one specific Step in a workflow the user drew. That Step
  called you for a bounded task and waits synchronously for your result.
- You read the same effective workspace as that Step and the rest of this
  workflow, under only your own frozen grant.
- Your final message returns only to the calling Step in its private history.
  The Step continues afterward, and Aura—not you—owns the final response.
- You cannot call helpers, delegate to agents, or run workflows. This helper
  relationship is exactly one level deep."""

WORKFLOW_HELPER_SHARED_WORKTREE_LAYER = """\
- This linked worktree already belongs to the whole workflow and is shared by
  its Steps and writable helpers. Aura did not create a separate worktree for
  you and will checkpoint the whole workflow exactly once after it ends."""


def child_workspace_facts(workspace_root: Path | str | None) -> str:
    """The minimal facts a child needs to address its effective workspace.

    Deliberately just the root and how paths are written. No repository map,
    no project profile, no file inventory, no capability packs: the child has
    read and search tools and can find the rest itself.
    """
    if workspace_root is None:
        return "Workspace: none is open. No files are readable."
    root = Path(workspace_root)
    return (
        "Workspace\n"
        f"- Root: {root}\n"
        f"- Name: {root.name}\n"
        "- Paths in tool calls are relative to the root unless stated otherwise."
    )


def compose_child_system_prompt(
    definition: AgentDefinition,
    *,
    workspace_root: Path | str | None,
    permission: AgentPermission = AgentPermission.READ_ONLY,
    change_set_id: str = "",
    base_sha: str = "",
    workflow_step: bool = False,
    workflow_step_helpers: bool = False,
    workflow_helper: bool = False,
) -> str:
    """Build the child's whole system prompt: host layer, brief, facts."""
    permission = AgentPermission(permission)
    if permission.allows_edit:
        host = _WRITABLE_HOST_LAYER
        if permission.allows_terminal:
            host += "\n" + _TERMINAL_HOST_LAYER
        if workflow_helper:
            host += "\n" + WORKFLOW_HELPER_SHARED_WORKTREE_LAYER
        lifecycle = (
            "Change set\n"
            f"- ID: {change_set_id}\n"
            f"- Frozen base: {base_sha}"
        )
    else:
        host = CHILD_HOST_LAYER
        lifecycle = ""
    if workflow_step:
        if workflow_step_helpers:
            host = host.replace(
                _NO_DELEGATION_LINE, _WORKFLOW_STEP_DELEGATION_LINE
            )
        host += "\n" + WORKFLOW_STEP_LAYER
        if workflow_step_helpers:
            host += "\n" + WORKFLOW_STEP_HELPERS_LAYER
    if workflow_helper:
        host += "\n" + WORKFLOW_HELPER_LAYER
    blocks = [
        host,
        (definition.instructions or "").strip(),
        child_workspace_facts(workspace_root),
        lifecycle,
    ]
    return "\n\n".join(block for block in blocks if block and block.strip())


def compose_child_task_message(task: str) -> str:
    """The child's single user message: the parent-authored task, verbatim."""
    return str(task or "").strip()


def compose_workflow_step_message(
    task: str,
    assignment: str,
    previous: Mapping[str, Any] | None = None,
    previous_agent: str = "",
) -> str:
    """The single user message one workflow step runs from.

    Three things, in the order they matter, and nothing else. The workflow's
    own task, unchanged for every step, so the last agent still knows what
    was actually asked. This occurrence's assignment, which is what *this*
    step is for. And the previous step's structured result, when there was one
    — the whole of it, because it is the only thing the next agent can see: a
    step's private history dies with it, so a fact the last agent left out of
    its result is a fact that no longer exists.
    """
    blocks = [f"Workflow task\n{str(task or '').strip()}"]
    brief = str(assignment or "").strip()
    if brief:
        blocks.append(f"Your step in this workflow\n{brief}")
    if previous:
        who = str(previous_agent or "").strip() or "the previous step"
        handed_on = json.dumps(dict(previous), ensure_ascii=False, indent=2)
        blocks.append(
            f"Structured result from {who}\n{handed_on}\n\n"
            "That is the only record of the work before you; nothing else of it "
            "survives. Verify anything you rely on."
        )
    return "\n\n".join(blocks)


def compose_workflow_helper_message(
    task: str,
    assignment: str,
    bounded_task: str,
    owning_step: str = "",
) -> str:
    """The original workflow ask plus this occurrence and Step-authored task."""
    blocks = [f"Workflow task\n{str(task or '').strip()}"]
    occurrence = str(assignment or "").strip()
    if occurrence:
        blocks.append(f"Your helper role in this workflow\n{occurrence}")
    owner = str(owning_step or "").strip() or "the calling workflow Step"
    blocks.append(
        f"Bounded task from {owner}\n{str(bounded_task or '').strip()}"
    )
    return "\n\n".join(blocks)


__all__ = [
    "CHILD_HOST_LAYER",
    "WORKFLOW_HELPER_LAYER",
    "WORKFLOW_STEP_LAYER",
    "WORKFLOW_STEP_HELPERS_LAYER",
    "child_workspace_facts",
    "compose_child_system_prompt",
    "compose_child_task_message",
    "compose_workflow_helper_message",
    "compose_workflow_step_message",
]
