"""Handler for the implementation decision record tool.

``record_implementation_decision`` is pure bookkeeping: it turns a model's
stated implementation decision into a structured, deterministically-identified
record that is returned as an ordinary tool result. It inspects nothing,
mutates nothing, and tracks no state of its own — History's chronology is what
orders successive decision records, so a later successful call supersedes an
earlier one without this handler needing to know that happened.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult

_DECISION_SEMANTICS = (
    "This is the current working implementation decision. Continue from it "
    "unless later evidence materially contradicts its basis, satisfies a "
    "reconsideration condition, an implementation or validation result "
    "disproves it, or the user changes the request. A later successful "
    "decision record supersedes earlier ones."
)


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for entry in value:
        text = _clean_str(entry)
        if text:
            cleaned.append(text)
    return cleaned


def _decision_id(
    decision: str,
    targets: list[str],
    basis: list[str],
    reconsider_if: list[str],
    validation: str,
) -> str:
    """Stable identity derived only from normalized semantic content.

    Canonical JSON (sorted keys, no whitespace ambiguity) over exactly the
    normalized fields, hashed with SHA-256. No timestamp, call order, random
    value, or process state ever enters the hash, so identical semantic input
    always yields the same id.
    """
    canonical = json.dumps(
        {
            "decision": decision,
            "targets": targets,
            "basis": basis,
            "reconsider_if": reconsider_if,
            "validation": validation,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DecisionHandlersMixin:
    """Static handler for ``record_implementation_decision``."""

    def _handle_record_implementation_decision(
        self,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        decision = _clean_str(args.get("decision"))
        validation = _clean_str(args.get("validation"))
        targets = _clean_list(args.get("targets"))
        basis = _clean_list(args.get("basis"))
        reconsider_if = _clean_list(args.get("reconsider_if"))

        missing: list[str] = []
        if not decision:
            missing.append("decision")
        if not basis:
            missing.append("basis")
        if not reconsider_if:
            missing.append("reconsider_if")
        if not validation:
            missing.append("validation")
        if missing:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        "record_implementation_decision requires non-empty "
                        f"{', '.join(missing)}."
                    ),
                    "failure_class": "invalid_arguments",
                },
            )

        decision_id = _decision_id(decision, targets, basis, reconsider_if, validation)
        payload: dict[str, Any] = {
            "ok": True,
            "kind": "implementation_decision",
            "decision_id": decision_id,
            "decision": decision,
            "targets": targets,
            "basis": basis,
            "reconsider_if": reconsider_if,
            "validation": validation,
            "semantics": _DECISION_SEMANTICS,
        }
        return ToolExecResult(
            ok=True, payload=payload, extras={"implementation_decision": True}
        )


__all__ = ["DecisionHandlersMixin"]
