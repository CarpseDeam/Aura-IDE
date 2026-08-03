"""Handler for ``commit_implementation_decision``.

The production single agent's explicit transition from repository discovery to
implementation.  It is bookkeeping in the strictest sense: it reads nothing,
writes nothing, touches no workspace state, and needs no approval.  All it does
is normalize the decision the turn has already reached into one compact
structured packet — objective, authoritative owners and seams, target files,
intended change, focused validation approach — and hand it back as a tool
result.

Two things then follow from that result, and neither of them lives here:

* :class:`~aura.conversation.focused_action.FocusedActionState` records that a
  decision was committed, and the send loop's *existing* focused-action
  protocol takes the next request.  This tool is not a second readiness
  manager; it is the positive signal the one owner reads.
* the completed call/result block is pinned turn context
  (:data:`~aura.conversation.api_view.PINNED_INSTRUCTION_TOOLS`), so it replays
  byte-identically for the rest of the real user turn.  That is what lets
  detailed source reads retire into the evidence ledger without the model
  losing the decision they supported.

Invalid arguments are an ordinary truthful failed tool result — a missing
objective is a malformed call, not a reason to end the turn — so the model can
read the failure and call it correctly.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult

#: What the model is told, in the durable result itself, about what happens
#: next.  Stated once, in the capsule that survives compaction, rather than
#: re-injected into canonical history every round.
DECISION_COMMITTED_NEXT = (
    "Discovery is over for this decision. The next request exposes only the "
    "editing tools and requires exactly one call: apply the change above. Do "
    "not survey further implementations, test runners, or executables first — "
    "validate and repair after the edit lands."
)


def _clean_text(raw: Any) -> str:
    return str(raw or "").strip()


def _clean_list(raw: Any) -> list[str]:
    """Normalize a string list: strip, drop empties, deduplicate, keep order."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        text = _clean_text(entry)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _clean_paths(raw: Any) -> list[str]:
    """Normalize target file paths: separators unified, otherwise untouched."""
    return _clean_list(
        [str(entry).replace("\\", "/") for entry in raw] if isinstance(raw, list) else raw
    )


def decision_identity(packet: dict[str, Any]) -> str:
    """Stable identity of one normalized decision packet.

    A content hash, not a counter: the same decision committed twice has the
    same identity, and a genuinely corrected decision has a different one.
    Nothing branches on how many identities a turn produced.
    """
    material = json.dumps(packet, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


class ImplementationDecisionHandlersMixin:
    """Provides ``commit_implementation_decision``."""

    def _handle_commit_implementation_decision(
        self,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        objective = _clean_text(args.get("objective"))
        owners = _clean_list(args.get("owners"))
        target_files = _clean_paths(args.get("target_files"))
        change = _clean_text(args.get("change"))
        validation = _clean_text(args.get("validation"))

        missing = [
            field
            for field, value in (
                ("objective", objective),
                ("owners", owners),
                ("target_files", target_files),
                ("change", change),
            )
            if not value
        ]
        if missing:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        "commit_implementation_decision requires non-empty "
                        + ", ".join(missing)
                        + ". Name the objective, the authoritative owners and "
                        "seams, the concrete target files, and the intended "
                        "change."
                    ),
                    "failure_class": "invalid_arguments",
                },
            )

        packet: dict[str, Any] = {
            "objective": objective,
            "owners": owners,
            "target_files": target_files,
            "change": change,
        }
        if validation:
            packet["validation"] = validation

        payload: dict[str, Any] = {
            "ok": True,
            "tool": "commit_implementation_decision",
            "implementation_decision_committed": True,
            "mutation": False,
            "applied": False,
            "decision_id": decision_identity(packet),
            "decision": packet,
            "next": DECISION_COMMITTED_NEXT,
        }
        return ToolExecResult(
            ok=True,
            payload=payload,
            extras={"implementation_decision_committed": True},
        )


__all__ = [
    "DECISION_COMMITTED_NEXT",
    "ImplementationDecisionHandlersMixin",
    "decision_identity",
]
