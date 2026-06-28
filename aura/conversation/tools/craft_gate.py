from __future__ import annotations

import difflib
import logging
import os
import time
from pathlib import Path

try:
    from aura.craft import (
        CraftEngine,
        ExplicitSpecContract,
        OwnershipContext,
        ProposalCapsule,
    )
except ImportError:
    CraftEngine = None
    ExplicitSpecContract = None
    OwnershipContext = None
    ProposalCapsule = None

from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.write_payloads import _mark_not_applied
from aura.conversation.tools.fs_write import _proposal_context

_log = logging.getLogger("aura.humanizer")


PATCH_FILE_CRAFT_REPAIR_ACTION = (
    "Re-read the current file and inspect proposed_context and craft_issues. Treat joined "
    "Python statements or swallowed newlines as a likely patch boundary issue. Retry "
    "patch_file with a larger enclosing block: the line before, the edited lines, and the "
    "line after. Use the current expected_file_hash. Keep existing-file recovery on "
    "patch_file; do not use write_file as a fallback for this existing-file edit."
)


def _compute_craft_line_ranges(proposal: dict) -> list[tuple[int, int]]:
    proposed_lines = proposal.get("new_content", "").splitlines()
    if proposal.get("is_new_file"):
        return [(1, len(proposed_lines) + 1)]

    old_content = proposal.get("old_content")
    new_content = proposal.get("new_content")
    if old_content is not None and new_content is not None:
        old_lines = old_content.splitlines()
        matcher = difflib.SequenceMatcher(None, old_lines, proposed_lines)
        ranges = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                ranges.append((j1 + 1, j2 + 1))
        return ranges
    return [(1, len(proposed_lines) + 1)]


def _issue_line(issue) -> int | None:
    value = getattr(issue, "line", None)
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _craft_context_line(
    hard_issues: list,
    issues: list,
    changed_line_ranges: list[tuple[int, int]],
) -> int | None:
    for issue in hard_issues:
        line = _issue_line(issue)
        if line is not None:
            return line
    for issue in issues:
        line = _issue_line(issue)
        if line is not None:
            return line
    for start_line, _end_line in changed_line_ranges:
        if isinstance(start_line, int) and start_line > 0:
            return start_line
    return None


def _run_craft_gate(
    proposal: dict,
    tool_name: str,
    contract: ExplicitSpecContract | None = None,
    workspace_root=None,
    task_shape=None,
) -> ToolExecResult | None:
    gate_started = time.perf_counter()

    def _finish_metadata(metadata: dict | None = None) -> dict:
        result = dict(metadata or {})
        result["craft_gate_ms"] = round((time.perf_counter() - gate_started) * 1000, 3)
        return result

    if CraftEngine is None or ProposalCapsule is None:
        return None

    env = os.environ.get("AURA_CRAFT", "1")
    if env == "0":
        return None

    observe_env = os.environ.get("AURA_CRAFT_OBSERVE", "0")
    is_observe = observe_env == "1"

    rel_path = proposal.get("rel_path", "")
    if not rel_path.endswith(".py"):
        return None

    try:
        is_new_file = proposal.get("is_new_file", False)
        task_shape_summary = _task_shape_summary(task_shape)
        ownership_context = OwnershipContext.AURA if (rel_path.startswith("aura/") and is_new_file) else OwnershipContext.FOREIGN
        changed_line_ranges = _compute_craft_line_ranges(proposal)
        capsule = ProposalCapsule(
            path=Path(rel_path),
            language="python",
            tool_name=tool_name,
            original_code=proposal.get("old_content", ""),
            proposed_code=proposal["new_content"],
            changed_line_ranges=changed_line_ranges,
            is_new_file=is_new_file,
            ownership_context=ownership_context,
            contract=contract,
            task_shape=task_shape,
        )

        if contract is not None:
            capsule.expected_public_symbols = list(getattr(contract, "expected_public_symbols", []))
            capsule.expected_dataclass_fields = dict(getattr(contract, "expected_dataclass_fields", {}))
            capsule.forbidden_public_methods = list(getattr(contract, "forbidden_public_methods", []))
            capsule.forbidden_calls = list(getattr(contract, "forbidden_calls", []))

        decision = CraftEngine().process_proposal(capsule)

        if is_observe:
            if not decision.approved:
                _log.info("[craft:observe] %s blocked", rel_path)
            return None

        metadata = _finish_metadata(getattr(decision, "metadata", {}) or {})
        if task_shape_summary:
            metadata.setdefault("task_shape", task_shape_summary)
        if decision.approved:
            proposal["new_content"] = decision.cleaned_code
            if metadata:
                proposal["craft_metadata"] = metadata
            proposal["write_outcome"] = str(metadata.get("write_outcome") or "applied")
            if metadata.get("checks_warned"):
                proposal["checks_warned"] = list(metadata.get("checks_warned") or [])
            elif decision.soft_issues:
                proposal["checks_warned"] = ["craft_engine"]
            warnings = metadata.get("craft_warnings")
            if warnings:
                proposal["craft_warnings"] = warnings
            elif decision.soft_issues:
                proposal["craft_warnings"] = [_craft_issue_payload(issue) for issue in decision.soft_issues]
            if metadata.get("pre_existing_environment_issues"):
                proposal["pre_existing_environment_issues"] = metadata.get("pre_existing_environment_issues")
            return None

        _log.info("[craft:block] %s blocked", rel_path)
        hard_issues = list(decision.hard_issues or [])
        issues = list(hard_issues or decision.issues)
        failure_class = str(metadata.get("failure_class") or "craft_blocked")
        if failure_class not in {"craft_blocked", "craft_rejected", "syntax_invalid"}:
            failure_class = "craft_rejected"
        ok = False
        context_line = _craft_context_line(hard_issues, issues, changed_line_ranges)
        payload = {
            "ok": ok,
            "error": _craft_block_error(issues),
            "path": rel_path,
            "rel_path": rel_path,
            "failure_class": failure_class,
            "syntax_valid": not any(getattr(issue, "code", "") == "syntax-error" for issue in issues),
            "is_new_file": is_new_file,
            "craft_issues": [_craft_issue_payload(issue) for issue in issues],
            "craft_metadata": metadata,
            "proposed_context": _proposal_context(proposal.get("new_content", ""), context_line),
        }
        if tool_name == "patch_file":
            payload["suggested_next_tool"] = "patch_file"
            payload["suggested_next_action"] = PATCH_FILE_CRAFT_REPAIR_ACTION
        return ToolExecResult(
            ok=ok,
            payload=_mark_not_applied(payload, failure_class)
        )
    except Exception:
        _log.exception("CraftEngine failed for %s", rel_path)
        proposal["craft_metadata"] = _finish_metadata({"checks_warned": ["craft_engine"]})
        proposal["checks_warned"] = ["craft_engine"]
        return None


def _task_shape_summary(task_shape) -> dict:
    if task_shape is None:
        return {}
    if hasattr(task_shape, "to_summary_dict"):
        try:
            summary = task_shape.to_summary_dict()
            return summary if isinstance(summary, dict) else {}
        except Exception:
            return {}
    task_kind = str(getattr(task_shape, "task_kind", "") or "")
    if not task_kind:
        return {}
    return {
        "task_kind": task_kind,
        "product_flow": list(getattr(task_shape, "product_flow", []) or getattr(task_shape, "core_flow", []) or []),
        "state_concepts": list(getattr(task_shape, "state_concepts", []) or []),
        "craft_pressure": list(getattr(task_shape, "craft_pressure", []) or []),
    }


def _craft_block_error(issues: list) -> str:
    if not issues:
        return "Craft blocked the proposed code before approval."
    first = issues[0]
    line = getattr(first, "line", None)
    code = getattr(first, "code", "craft")
    message = getattr(first, "message", "Craft blocked the proposed code before approval.")
    prefix = f"Line {line}: " if line else ""
    return f"{prefix}{code}: {message}"


def _craft_issue_payload(issue) -> dict:
    severity = getattr(issue, "severity", "")
    return {
        "line": getattr(issue, "line", None),
        "column": getattr(issue, "column", None),
        "code": getattr(issue, "code", ""),
        "message": getattr(issue, "message", ""),
        "suggestion": getattr(issue, "suggestion", ""),
        "severity": getattr(severity, "value", str(severity)),
    }
