"""Worker final report proof guard."""
from __future__ import annotations

import re
from typing import Any

from aura.conversation.completion_guard import assistant_message_text


WORKER_FINAL_REPORT_PROOF_REQUIRED_TEXT = (
    "Worker final report is missing explicit validation or acceptance proof. "
    "Reply with the final report only and include concrete lines for changed files, "
    "validation command/result, and acceptance verification."
)

_FINAL_REPORT_INCOMPLETE_PROOF_RE = re.compile(
    r"\b(?:not\s+(?:tested|validated|verified)|validation\s+(?:did\s+not|didn't|"
    r"not)\s+run|failed\s+(?:validation|acceptance)|(?:validation|acceptance)\s+failed|"
    r"could\s+not\s+(?:verify|run)|couldn't\s+(?:verify|run)|unable\s+to\s+(?:verify|run))\b",
    re.IGNORECASE,
)

_FINAL_REPORT_VALIDATION_PROOF_RE = re.compile(
    r"\b(?:verified|validated|pytest|py_compile|compileall|ruff|mypy|tests?\s+pass(?:ed|es)?|"
    r"compiled|exit\s+code\s+0|exits\s+0)\b",
    re.IGNORECASE,
)

_FINAL_REPORT_ACCEPTANCE_PROOF_RE = re.compile(
    r"\b(?:acceptance|accepted)\b.{0,80}\b(?:verified|validated|passed|met|satisfied|confirmed|ok)\b|"
    r"\b(?:verified|validated|passed|met|satisfied|confirmed)\b.{0,80}\bacceptance\b",
    re.IGNORECASE | re.DOTALL,
)


def _worker_final_report_claims_validation_or_acceptance(content: str) -> bool:
    text = str(content or "")
    if _FINAL_REPORT_INCOMPLETE_PROOF_RE.search(text):
        return False
    return bool(
        _FINAL_REPORT_VALIDATION_PROOF_RE.search(text)
        or _FINAL_REPORT_ACCEPTANCE_PROOF_RE.search(text)
    )


def _worker_final_report_needs_proof(state: Any) -> bool:
    flow = state.worker_flow
    write_actions = int(getattr(flow.state, "write_actions", 0) or 0) if flow else 0
    return bool(
        write_actions > 0
        or getattr(state, "worker_app_writes", set())
        or getattr(state, "syntax_validation_required", set())
    )


def worker_final_report_missing_proof(
    state: Any,
    full_message: dict[str, Any],
    *,
    ignore_prior_nudge: bool = False,
) -> bool:
    if state.worker_final_report_proof_nudge_sent and not ignore_prior_nudge:
        return False
    if not _worker_final_report_needs_proof(state):
        return False
    return not _worker_final_report_claims_validation_or_acceptance(
        assistant_message_text(full_message)
    )
