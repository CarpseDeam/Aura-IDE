"""The compact agent roster block Aura's own prompt carries.

This is the only place agents are described to the root model in prose, and it
says the least that is useful: who is available, by id and name, and the one
line their author wrote about what they are for.

Two rules make it safe to grow a roster:

* an agent's **full instructions never appear here** — they are the child's
  brief, delivered to the child alone;
* the block **does not exist at all** when the roster is empty, so a user who
  has made no agent available runs exactly the prompt they always ran.
"""
from __future__ import annotations

from typing import Iterable

_HEADER = "### Agents"

_GUIDANCE = (
    "These agents are available to you through `delegate_agent`. Each runs "
    "read-only, in the foreground, on its own private conversation: it cannot "
    "see this one, cannot edit anything, and cannot ask a follow-up question. "
    "Delegate when a bounded investigation is genuinely better done separately, "
    "write the whole task in the call, and do the work yourself otherwise."
)


def format_agent_roster_block(rows: Iterable[dict[str, str]]) -> str:
    """Render the roster block, or ``""`` when there is nothing to render."""
    lines: list[str] = []
    for row in rows or ():
        agent_id = str(row.get("agent_id") or "").strip()
        if not agent_id:
            continue
        name = str(row.get("name") or "").strip() or agent_id
        description = str(row.get("description") or "").strip()
        permission = str(row.get("permission_label") or "").strip()
        suffix = f" [{permission}]" if permission else ""
        lines.append(
            f"- `{agent_id}` — **{name}**{suffix}: {description}"
            if description
            else f"- `{agent_id}` — **{name}**{suffix}"
        )
    if not lines:
        return ""
    return "\n".join([_HEADER, "", _GUIDANCE, ""] + lines)


__all__ = ["format_agent_roster_block"]
