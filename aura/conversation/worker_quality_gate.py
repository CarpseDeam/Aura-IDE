from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path, PurePosixPath
from typing import Callable

from aura.client import Done, Event
from aura.conversation.critic_dispatch import CriticCallback, CriticRequest
from aura.conversation.critic_verdict import CriticFinding, CriticVerdict
from aura.conversation.dispatch import WorkerDispatchRequest, WorkerMismatch
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.worker_fingerprints import fingerprint_paths
from aura.conversation.worker_quality import (
    PROTECTED_CONTROL_FLOW_FILES,
    evaluate_worker_quality,
    findings_to_receipt,
)

EventCallback = Callable[[Event], None]
_log = logging.getLogger(__name__)


def handle_worker_quality_gate(
    *,
    state: _SendState,
    workspace_root,
    history: History,
    on_event: EventCallback,
    critic_cb: CriticCallback | None = None,
    worker_request: WorkerDispatchRequest | None = None,
    dispatch_tool_call_id: str = "",
) -> str:
    if not state.worker_quality_enabled:
        return "none"
    changed_files = sorted(state.worker_app_writes)
    if not changed_files:
        return "none"
    root = Path(workspace_root)
    if not (root / ".git").exists():
        return "none"

    fingerprint = fingerprint_paths(set(changed_files), root)
    if fingerprint and fingerprint == state.last_quality_ok_fingerprint:
        return "none"

    diff_text = _diff_changed_files(root, changed_files)
    decision = evaluate_worker_quality(
        root,
        changed_files,
        diff_text,
        validation_passed=True,
        expected_files=state.dispatched_target_files or None,
    )
    state.last_quality_findings = findings_to_receipt(decision.findings)

    if decision.hard_block:
        # Hard block becomes repeatable feedback, not terminal.
        if decision.instruction:
            history.append_user_text(decision.instruction)
        else:
            history.append_user_text(
                "Quality gate: structural review identified issues. "
                "Review the findings below and address them."
            )
        return "cleanup"

    if decision.needs_cleanup:
        history.append_user_text(decision.instruction)
        return "cleanup"

    if (
        critic_cb is not None
        and worker_request is not None
        and _critic_risk_triggered(changed_files)
    ):
        verdict = _invoke_critic(
            critic_cb=critic_cb,
            dispatch_tool_call_id=dispatch_tool_call_id,
            worker_request=worker_request,
            workspace_root=root,
            changed_files=changed_files,
            diff_text=diff_text,
        )
        state.last_quality_findings = _critic_findings_to_receipt(verdict.findings)
        if verdict.route == "worker":
            history.append_user_text(
                verdict.instruction or _critic_cleanup_instruction(verdict.findings)
            )
            return "cleanup"
        if verdict.route == "planner":
            # Planner route is only terminal when it carries concrete conflict evidence.
            if verdict.planner_question and _has_concrete_conflict_evidence(verdict):
                _finish_worker_critic_planner_resolution(
                    history=history,
                    on_event=on_event,
                    worker_request=worker_request,
                    verdict=verdict,
                )
                return "finished"
            # Otherwise give feedback and continue.
            history.append_user_text(
                verdict.instruction or _critic_cleanup_instruction(verdict.findings)
            )
            return "cleanup"

    if fingerprint:
        state.last_quality_ok_fingerprint = fingerprint
    state.last_quality_findings = []
    return "none"


def _warning_only_findings(findings: list[dict]) -> bool:
    return bool(findings) and all(
        isinstance(finding, dict)
        and str(finding.get("severity") or "") == "warning"
        for finding in findings
    )


def _diff_changed_files(workspace_root: Path, changed_files: list[str]) -> str:
    if not changed_files:
        return ""
    files = sorted(changed_files)
    for revision_args in (
        ["HEAD"],
        ["HEAD~1", "HEAD"],
    ):
        result = subprocess.run(
            ["git", "-C", str(workspace_root), "diff", *revision_args, "--", *files],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            continue
        if result.stdout:
            return result.stdout
    return ""


def _critic_risk_triggered(changed_files: list[str]) -> bool:
    normalized = _normalize_changed_files(changed_files)
    if len(normalized) > 2:
        return True
    if any(_is_gui_file(path) for path in normalized):
        return True
    return any(Path(path).name in PROTECTED_CONTROL_FLOW_FILES for path in normalized)


def _normalize_changed_files(changed_files: list[str]) -> list[str]:
    return sorted({
        str(path).replace("\\", "/").lstrip("/")
        for path in changed_files
        if str(path).strip()
    })


def _is_gui_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return "gui" in parts


def _has_concrete_conflict_evidence(verdict: CriticVerdict) -> bool:
    """Check if the critic verdict has concrete conflicting/impossible spec evidence."""
    if any(
        finding.clause in {"conflicting_spec", "impossible_spec", "contradictory_requirements"}
        for finding in verdict.findings
    ):
        return True
    return False


def _invoke_critic(
    *,
    critic_cb: CriticCallback,
    dispatch_tool_call_id: str,
    worker_request: WorkerDispatchRequest,
    workspace_root: Path,
    changed_files: list[str],
    diff_text: str,
) -> CriticVerdict:
    try:
        return critic_cb(
            dispatch_tool_call_id,
            CriticRequest(
                original_request=worker_request,
                diff_text=diff_text,
                workspace_root=workspace_root,
                changed_files=changed_files,
            ),
        )
    except Exception:
        _log.debug("worker_quality_critic_callback_failed_open", exc_info=True)
        return CriticVerdict.release()


def _critic_findings_to_receipt(findings: list[CriticFinding]) -> list[dict]:
    return [finding.to_dict() for finding in findings]


def _critic_cleanup_instruction(findings: list[CriticFinding]) -> str:
    lines = [
        "Patch only the critic conformance findings.",
        "Do not redesign.",
        "Do not broaden scope.",
        "Rerun the smallest relevant validation.",
        "",
        "Findings:",
    ]
    for finding in findings:
        location = finding.file or "<workspace>"
        lines.append(
            f"- {location} - {finding.clause}: {finding.message} - {finding.suggested_action}"
        )
    return "\n".join(lines)


def _finish_worker_critic_planner_resolution(
    *,
    history: History,
    on_event: EventCallback,
    worker_request: WorkerDispatchRequest,
    verdict: CriticVerdict,
) -> None:
    files = sorted({finding.file for finding in verdict.findings if finding.file})
    first_clause = verdict.findings[0].clause if verdict.findings else worker_request.spec
    observed = "\n".join(
        f"{finding.file or '<workspace>'}: {finding.message}"
        for finding in verdict.findings
    )
    planner_question = (
        verdict.planner_question
        or "Please revise the worker handoff so the implementation target is achievable and unambiguous."
    )
    payload = {
        "status": "harness_error",
        "summary": "Critic found the worker diff needs planner resolution.",
        "mismatch": {
            "kind": WorkerMismatch.CONFLICTING_SPEC,
            "file_paths": files,
            "requested": first_clause,
            "observed": observed,
            "worker_recommendation": (
                "Revise the dispatch request or clarify the conflicting acceptance clause."
            ),
            "question_for_planner": planner_question,
        },
        "critic_findings": _critic_findings_to_receipt(verdict.findings),
    }
    content = json.dumps(payload, ensure_ascii=False)
    full_message = {
        "role": "assistant",
        "content": content,
        "reasoning_content": None,
    }
    history.append_assistant(full_message)
    on_event(Done(finish_reason="stop", full_message=full_message))


