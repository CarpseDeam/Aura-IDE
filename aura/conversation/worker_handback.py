"""Detect and build internal worker handback payloads.

Worker recoverable failures that carry planner-resolution metadata are
converted to internal dispatch continuations instead of visible terminal
messages.  The dispatch proxy then routes these through the same
``is_internal_dispatch_continuation`` lifecycle as campaign handbacks
and spec-reject results — no visible card, no user-facing Done, no
half-finished summary.

Architecture boundary
---------------------
This module is the single source of truth for:
  - What metadata signals an internal handback (``should_route_as_internal_handback``).
  - How to build the assistant message so the dispatch proxy treats it as
    Planner control-flow, not an error (``build_internal_handback_message``).
  - What extras to add to the ``WorkerDispatchResult`` so the Planner-side
    lifecycle predicates route it as an invisible continuation
    (``annotate_worker_result_extras``).

The manager calls ``should_route_as_internal_handback`` + ``build_internal_handback_message``
inside ``_finish_worker_recoverable_followup``.  The dispatch proxy calls
``annotate_worker_result_extras`` in ``_run_worker``.  No other layer
needs to change.
"""
from __future__ import annotations

import json
from typing import Any

__all__ = [
    "INTERNAL_HANDBACK_MARKERS",
    "annotate_worker_result_extras",
    "build_internal_handback_message",
    "should_route_as_internal_handback",
]

# ── Detection markers ───────────────────────────────────────────────────
# Presence of any of these in a recoverable-followup *details* dict signals
# that the Worker should hand back to the Planner silently rather than
# emitting a visible terminal message.

INTERNAL_HANDBACK_MARKERS: frozenset[str] = frozenset({
    "planner_resolution_needed",
    "internal_planner_handoff",
    "suppress_user_followup_card",
})


# ── Public predicates ───────────────────────────────────────────────────

def should_route_as_internal_handback(details: dict[str, Any] | None) -> bool:
    """Return True when *details* signals an internal handback.

    Checks for:
    - ``planner_resolution_needed`` / ``internal_planner_handoff`` /
      ``suppress_user_followup_card`` in the marker set.
    - ``suggested_next_tool`` set to ``dispatch_to_worker``.
    - A non-empty ``worker_confusion_question``.

    These cover every worker-recoverable-followup path that should result
    in an invisible Planner restart: syntax-exhaustion, edit-mechanics-
    exhaustion, validation-exhaustion, zero-work-no-progress, flow-thrash,
    and validation-required-after-nudge.
    """
    if not isinstance(details, dict):
        return False
    if any(details.get(marker) for marker in INTERNAL_HANDBACK_MARKERS):
        return True
    if details.get("suggested_next_tool") == "dispatch_to_worker":
        return True
    if details.get("worker_confusion_question"):
        return True
    return False


# ── Message construction ────────────────────────────────────────────────

def build_internal_handback_message(
    *,
    failure_class: str,
    error: str,
    details: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build ``(content_json, full_message)`` for an internal handback.

    Sets ``status=needs_planner_resolution`` so the dispatch proxy's
    ``_parse_structured_worker_failure`` promotes this into Planner
    control-flow instead of appending it to ``result_errors``.  The
    failure constraint is embedded in *details* so the lifecycle predicates
    generate a bounded retry constraint.
    """
    payload: dict[str, Any] = {
        "ok": False,
        "recoverable": True,
        "needs_follow_up": True,
        "status": "needs_planner_resolution",
        "failure_class": failure_class,
        "error": error,
    }
    if details:
        payload["details"] = dict(details)
    content = json.dumps(payload, ensure_ascii=False)
    full_message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "reasoning_content": None,
    }
    return content, full_message


# ── Extras annotation ───────────────────────────────────────────────────

def annotate_worker_result_extras(
    extras: dict[str, Any],
    *,
    status: str | None,
    structured_failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add internal-handback extras when *status* indicates Planner control-flow.

    Sets ``internal_planner_handoff``, ``suppress_user_followup_card``,
    ``user_visible_blocker``, and ``failure_constraint`` on the extras
    dict so the Planner-side lifecycle predicates (``dispatch_lifecycle``)
    route this result as an invisible internal continuation.

    The caller (``_run_worker`` in the dispatch proxy) reassigns *extras*
    from the returned dict before constructing the ``WorkerDispatchResult``.
    When *status* does not indicate an internal handback the original
    *extras* dict is returned unchanged.
    """
    if status != "needs_planner_resolution":
        return extras

    details: dict[str, Any] = {}
    if isinstance(structured_failure, dict):
        raw_details = structured_failure.get("details")
        if isinstance(raw_details, dict):
            details = raw_details

    # Copy so we never mutate the caller's dict.
    result = dict(extras)
    result.setdefault("internal_planner_handoff", True)
    result.setdefault("suppress_user_followup_card", True)
    result.setdefault("user_visible_blocker", False)

    if not result.get("failure_constraint"):
        constraint = _build_failure_constraint(
            str(details.get("failure_class") or ""),
            details,
        )
        if constraint:
            result["failure_constraint"] = constraint

    return result


# ── Internal helpers ────────────────────────────────────────────────────

def _build_failure_constraint(failure_class: str, details: dict[str, Any]) -> str:
    """Build a Planner-facing failure constraint from handback metadata.

    Uses ``suggested_next_action`` as the primary source, falls back to
    ``worker_confusion_question``, then to a generic constraint derived
    from *failure_class*.
    """
    suggested_action = str(details.get("suggested_next_action") or "").strip()
    worker_confusion = str(details.get("worker_confusion_question") or "").strip()

    parts = ["CONSTRAINT FOR NEXT ATTEMPT:"]

    if worker_confusion:
        parts.append(worker_confusion)
        if suggested_action and suggested_action not in worker_confusion:
            parts.append(suggested_action)
    elif suggested_action:
        parts.append(suggested_action)
    else:
        parts.append(
            f"Previous worker attempt failed: {failure_class}. "
            "Revise the approach, narrow the target, or provide more "
            "specific edit guidance before retrying."
        )

    return " ".join(parts)
