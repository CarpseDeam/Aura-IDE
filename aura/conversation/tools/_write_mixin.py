"""Mixin providing write handler methods for ToolRegistry.

Expected on self:
    _root: Path  (workspace root)
    _read_only: bool
    _mode: RegistryMode
    _resolve_in_root(path: str) -> Path  (method on ToolRegistry)

Functions are looked up through *registry* at call time so that
``unittest.mock.patch("aura.conversation.tools.registry.<name>")``
in test_tool_registry.py takes effect correctly.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

from aura.conversation.tools._types import ApprovalRequest, ToolExecResult
from aura.paths import safe_relative_to

# Import the registry module so we can look up functions at call time.
# This creates a circular import, but Python handles it because
# `registry` is already in sys.modules by the time this module is loaded.

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

from aura.conversation.tools import registry as _reg

_log = logging.getLogger("aura.humanizer")


def _humanizer_settings():
    try:
        from aura.settings import load_settings
        return load_settings()
    except Exception:
        return None


def _humanizer_enabled() -> bool:
    settings = _humanizer_settings()
    enabled = True if settings is None else bool(getattr(settings, "humanizer_enabled", True))

    env = os.environ.get("AURA_HUMANIZER")
    if env == "0":
        return False
    if env == "1":
        return True

    return enabled


def _humanizer_observe_enabled() -> bool:
    settings = _humanizer_settings()
    observe = False if settings is None else bool(getattr(settings, "humanizer_observe", False))

    env = os.environ.get("AURA_HUMANIZER_OBSERVE")
    if env == "1":
        return True
    if env == "0":
        return False

    return observe


def _humanizer_feature_log_enabled() -> bool:
    settings = _humanizer_settings()
    enabled = False if settings is None else bool(getattr(settings, "humanizer_feature_log", False))

    env = os.environ.get("AURA_HUMANIZER_FEATURE_LOG")
    if env == "1":
        return True
    if env == "0":
        return False

    return enabled


def _humanizer_gate_enabled() -> bool:
    # Tool-result based rejection is developer-only because normal tool results
    # enter model-visible history and UI event flow. Product behavior is
    # cleanup/scan before approval, not visible rejection.
    if not _humanizer_enabled():
        return False
    if _humanizer_observe_enabled():
        return False

    settings = _humanizer_settings()
    enabled = False if settings is None else bool(getattr(settings, "humanizer_gate_enabled", False))

    env = os.environ.get("AURA_HUMANIZER_GATE")
    if env == "0":
        return False
    if env == "1":
        return True

    return enabled


def _humanizer_gate_min_severity() -> str:
    settings = _humanizer_settings()
    severity = "high" if settings is None else str(getattr(settings, "humanizer_gate_min_severity", "high")).lower()

    env = os.environ.get("AURA_HUMANIZER_GATE_MIN_SEVERITY")
    if env:
        severity = env.strip().lower()

    if severity not in {"critical", "high", "medium", "low"}:
        return "high"

    return severity


def _log_humanizer_observe(rel_path: str, result) -> None:
    """Log what the humanizer would change for observe-only mode."""
    if result.changed:
        parts = []
        if result.markdown_stripped:
            parts.append("strip markdown")
        if result.comments_removed > 0:
            parts.append(f"remove {result.comments_removed} comments")
        if result.docstrings_removed > 0:
            parts.append(f"remove {result.docstrings_removed} docstrings")
        _log.info("[humanizer:observe] %s: would %s", rel_path, ", ".join(parts))
    else:
        _log.info("[humanizer:observe] %s: no changes", rel_path)

    if _humanizer_feature_log_enabled() and result.feature_report and result.feature_report.has_structural_smells:
        report = result.feature_report
        _log.info(
            "[humanizer:features] %s: %d tuple returns, %d generic names, %d narration comments, %d thin helpers",
            rel_path,
            len(report.tuple_returns),
            len(report.generic_names),
            len(report.narration_comments),
            len(report.thin_helpers),
        )


def _severity_rank(value: str) -> int:
    order = {
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
    }
    return order.get(value, 99)


def _blocking_slop_issues(result) -> list:
    """Return blocking slop issues from a HumanizerResult, sorted by severity."""
    report = getattr(result, "slop_report", None)
    if report is None:
        return []

    min_severity = _humanizer_gate_min_severity()

    blocking = []
    for issue in report.issues:
        severity = getattr(issue.severity, "value", str(issue.severity)).lower()

        if severity == "critical":
            blocking.append(issue)
            continue

        if min_severity == "high" and severity == "high":
            blocking.append(issue)

    blocking.sort(
        key=lambda issue: (
            _severity_rank(getattr(issue.severity, "value", str(issue.severity)).lower()),
            getattr(issue, "line", 0),
            getattr(issue, "column", 0),
            getattr(issue, "code", ""),
        )
    )
    return blocking


def _humanizer_gate_error(rel_path: str, result, blocking_issues: list) -> ToolExecResult:
    """Build a ToolExecResult rejection for blocking slop issues."""
    report = getattr(result, "slop_report", None)

    issues_payload = []
    for issue in blocking_issues[:8]:
        severity = getattr(issue.severity, "value", str(issue.severity))
        issues_payload.append(
            {
                "code": issue.code,
                "line": issue.line,
                "severity": severity,
                "message": issue.message,
                "suggestion": issue.suggestion,
            }
        )

    return ToolExecResult(
        ok=False,
        payload=_mark_not_applied({
            "ok": False,
            "error": "Aura humanizer rejected generated Python before approval.",
            "failure_class": "craft_rejected",
            "humanizer_gate": True,
            "path": rel_path,
            "slop_score": getattr(report, "score", 0.0) if report else 0.0,
            "slop_status": getattr(report, "status", "unknown") if report else "unknown",
            "issue_count": getattr(report, "issue_count", 0) if report else 0,
            "blocking_issue_count": len(blocking_issues),
            "issues": issues_payload,
        }),
    )


def _maybe_observe_humanizer(proposal: dict) -> None:
    """Run humanizer in observe-only mode for existing .py file edits."""
    if not _humanizer_enabled():
        return
    if os.environ.get("AURA_HUMANIZER_EDIT_FILE") != "1":
        return
    rel_path = proposal.get("rel_path", "")
    if not rel_path.endswith(".py"):
        return
    try:
        from aura.humanizer import HumanizerPipeline

        result = HumanizerPipeline().humanize_code(
            proposal["new_content"], language="python"
        )
        _log_humanizer_observe(rel_path, result)
    except Exception:
        _log.exception(
            "HumanizerPipeline failed for %s, skipping observe", rel_path
        )


def _maybe_humanize_proposal(proposal: dict) -> ToolExecResult | None:
    """Run humanizer on proposal content, potentially replacing it.

    Respects AURA_HUMANIZER kill switch and AURA_HUMANIZER_OBSERVE mode.
    Returns a rejection ToolExecResult when the gate blocks slop, else None.
    All other errors are logged and swallowed.
    """
    if not _humanizer_enabled():
        return None
    rel_path = proposal.get("rel_path", "")
    if not rel_path.endswith(".py"):
        return None
    try:
        from aura.humanizer import HumanizerPipeline

        pipeline_path = Path(rel_path) if rel_path else None
        result = HumanizerPipeline().humanize_code(
            proposal["new_content"], language="python", path=pipeline_path
        )
        if _humanizer_observe_enabled():
            _log_humanizer_observe(rel_path, result)
        else:
            if not result.syntax_fallback and result.error is None:
                proposal["new_content"] = result.text

        # Gate: reject if blocking slop issues found
        if _humanizer_gate_enabled():
            blocking_issues = _blocking_slop_issues(result)
            if blocking_issues:
                return _humanizer_gate_error(rel_path, result, blocking_issues)

        if _humanizer_feature_log_enabled() and result.feature_report and result.feature_report.has_structural_smells:
            report = result.feature_report
            _log.info(
                "[humanizer:features] %s: %d tuple returns, %d generic names, %d narration comments, %d thin helpers",
                rel_path,
                len(report.tuple_returns),
                len(report.generic_names),
                len(report.narration_comments),
                len(report.thin_helpers),
            )
            for tr in report.tuple_returns:
                _log.info(
                    "[humanizer:features] %s: %s returns %d values on line %d",
                    rel_path, tr.function_name, tr.size, tr.line,
                )
            for gn in report.generic_names:
                _log.info(
                    "[humanizer:features] %s: generic name '%s' on line %d",
                    rel_path, gn.name, gn.line,
                )
            for nc in report.narration_comments:
                _log.info(
                    "[humanizer:features] %s: narration comment on line %d: %s",
                    rel_path, nc.line, nc.text[:60],
                )
            for th in report.thin_helpers:
                _log.info(
                    "[humanizer:features] %s: thin helper '%s' (%d lines) on line %d",
                    rel_path, th.function_name, th.body_lines, th.line,
                )
    except Exception:
        _log.exception(
            "HumanizerPipeline failed for %s, using original content", rel_path
        )
    return None



import difflib


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



def _run_craft_gate(
    proposal: dict,
    tool_name: str,
    contract: ExplicitSpecContract | None = None,
    workspace_root=None,
    task_shape=None,
) -> ToolExecResult | None:
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
        capsule = ProposalCapsule(
            path=Path(rel_path),
            language="python",
            tool_name=tool_name,
            original_code=proposal.get("old_content", ""),
            proposed_code=proposal["new_content"],
            changed_line_ranges=_compute_craft_line_ranges(proposal),
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
            
        metadata = dict(getattr(decision, "metadata", {}) or {})
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
        issues = list(decision.hard_issues or decision.issues)
        failure_class = str(metadata.get("failure_class") or "craft_blocked")
        if failure_class not in {"craft_blocked", "craft_rejected", "syntax_invalid"}:
            failure_class = "craft_rejected"
        ok = False
        return ToolExecResult(
            ok=ok,
            payload=_mark_not_applied(
                {
                    "ok": ok,
                    "error": _craft_block_error(issues),
                    "path": rel_path,
                    "rel_path": rel_path,
                    "failure_class": failure_class,
                    "syntax_valid": not any(getattr(issue, "code", "") == "syntax-error" for issue in issues),
                    "is_new_file": is_new_file,
                    "craft_issues": [_craft_issue_payload(issue) for issue in issues],
                    "craft_metadata": metadata,
                },
                failure_class,
            )
        )
    except Exception:
        _log.exception("CraftEngine failed for %s", rel_path)
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


def _write_outcome_for_failure(failure_class: str) -> str:
    if failure_class == "approval_rejected":
        return "not_applied_user_rejected"
    if failure_class in {"craft_blocked", "craft_rejected", "introduced_environment_issue", "syntax_invalid"}:
        return "not_applied_craft_rejected"
    if failure_class == "pre_existing_environment_issue":
        return "not_applied_pre_existing_environment_blocked"
    if failure_class == "internal_error":
        return "failed_harness_error"
    return "not_applied_edit_mechanics_blocked"


def _mark_not_applied(payload: dict, failure_class: str | None = None) -> dict:
    payload.setdefault("applied", False)
    if failure_class:
        payload.setdefault("failure_class", failure_class)
    payload.setdefault(
        "write_outcome",
        _write_outcome_for_failure(str(payload.get("failure_class") or failure_class or "")),
    )
    return payload


def _mark_delete_not_applied(payload: dict, failure_class: str | None = None) -> dict:
    payload = _mark_not_applied(payload, failure_class)
    payload.setdefault("deleted", False)
    return payload


def _is_delete_protected_path(rel_path: str) -> bool:
    normalized = _normalize_worker_path(rel_path).lstrip("/")
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized == ".git"
        or normalized.startswith(".git/")
        or normalized == ".aura"
        or normalized.startswith(".aura/")
        or name == ".env"
        or name.startswith(".env.")
    )


def _python_syntax_error_payload(proposal: dict) -> dict | None:
    path = str(proposal.get("rel_path") or proposal.get("path") or "")
    if not path.endswith(".py"):
        return None
    try:
        compile(str(proposal.get("new_content") or ""), path or "<proposal>", "exec")
    except SyntaxError as exc:
        return _mark_not_applied(
            {
                "ok": False,
                "path": path,
                "rel_path": path,
                "error": f"replacement produces invalid Python: {exc}",
                "failure_class": "syntax_invalid",
                "syntax_valid": False,
                "suggested_next_tool": "apply_edit_transaction",
                "suggested_next_action": (
                    "Repair the Python syntax in this file before any unrelated tool call."
                ),
            },
            "syntax_invalid",
        )
    return None


def _is_new_root_validation_scratch(root: Path, target: Path) -> bool:
    return (
        target.parent == root
        and not target.exists()
        and _is_scratch_python_name(target.name)
        and target.suffix == ".py"
    )



def _normalize_worker_path(path: str) -> str:
    normalized = str(path).replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _is_validation_scratch_path(path: str) -> bool:
    normalized = _normalize_worker_path(path)
    name = normalized.rsplit("/", 1)[-1]
    if not name.endswith(".py"):
        return False
    if normalized.startswith(".aura/tmp/") or "/" not in normalized:
        return _is_scratch_python_name(name)
    return False


def _is_aura_tmp_scratch_path(path: str) -> bool:
    normalized = _normalize_worker_path(path)
    return normalized.startswith(".aura/tmp/") and _is_validation_scratch_path(normalized)


def _is_scratch_python_name(name: str) -> bool:
    return name.startswith(
        (
            "dump",
            "_check",
            "check",
            "tmp",
            "_tmp",
            "_inspect",
            "inspect",
            "diagnostic",
            "_diagnostic",
        )
    )


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    temp_path: Path | None = None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        if target.exists():
            os.chmod(temp_path, stat.S_IMODE(target.stat().st_mode))
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


class WriteHandlersMixin:
    """Handlers for write tools — guards + approval + backup."""

    def _handle_write_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_write("write_file", args, approval_cb, reject_all)

    def _handle_delete_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_delete_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended deletion in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_delete(args, approval_cb, reject_all)

    def _handle_apply_edit_transaction(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_write("apply_edit_transaction", args, approval_cb, reject_all)

    def _handle_edit_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_write("edit_file", args, approval_cb, reject_all)

    def _handle_edit_symbol(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_write("edit_symbol", args, approval_cb, reject_all)

    def _handle_edit_line_range(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_write("edit_line_range", args, approval_cb, reject_all)

    def _handle_patch_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload=_mark_not_applied({"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled.", "failure_class": "read_only"}))
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied({
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                    "failure_class": "internal_error",
                }),
            )
        return self._handle_write("patch_file", args, approval_cb, reject_all)

    def _handle_delete(
        self,
        args: dict,
        approval_cb,
        reject_all: bool,
    ) -> ToolExecResult:
        reason = args.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "error": "reason must be a string",
                    "failure_class": "delete_file_invalid_path",
                    "reason": "",
                }, "delete_file_invalid_path"),
            )
        if reject_all:
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied(
                    {"ok": False, "error": "User rejected all writes in this turn.", "failure_class": "approval_rejected", "reason": reason},
                    "approval_rejected",
                ),
                extras={"rejected_all": True},
            )

        path_arg = args.get("path", "")
        if not isinstance(path_arg, str) or not path_arg.strip():
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": path_arg if isinstance(path_arg, str) else "",
                    "error": "path must be a non-empty string",
                    "failure_class": "delete_file_invalid_path",
                    "reason": reason,
                }, "delete_file_invalid_path"),
            )
        if any(char in path_arg for char in "*?[]"):
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": path_arg,
                    "error": "delete_file does not accept globs or wildcard paths",
                    "failure_class": "delete_file_invalid_path",
                    "reason": reason,
                }, "delete_file_invalid_path"),
            )
        try:
            target = self._resolve_in_root(path_arg)
        except ValueError as exc:
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": path_arg,
                    "error": str(exc),
                    "failure_class": "delete_file_workspace_escape",
                    "reason": reason,
                }, "delete_file_workspace_escape"),
            )

        rel_path = safe_relative_to(target, self._root).as_posix()
        if _is_delete_protected_path(rel_path):
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": rel_path,
                    "rel_path": rel_path,
                    "error": "delete_file cannot delete protected workspace metadata or environment files",
                    "failure_class": "delete_file_protected_path",
                    "reason": reason,
                }, "delete_file_protected_path"),
            )
        if not target.exists():
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": rel_path,
                    "rel_path": rel_path,
                    "error": "delete_file target does not exist",
                    "failure_class": "delete_file_missing",
                    "reason": reason,
                }, "delete_file_missing"),
            )
        if target.is_dir():
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": rel_path,
                    "rel_path": rel_path,
                    "error": "delete_file cannot delete directories",
                    "failure_class": "delete_file_is_directory",
                    "reason": reason,
                }, "delete_file_is_directory"),
            )
        if not target.is_file():
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied({
                    "ok": False,
                    "path": rel_path,
                    "rel_path": rel_path,
                    "error": "delete_file target must be a regular file",
                    "failure_class": "delete_file_invalid_path",
                    "reason": reason,
                }, "delete_file_invalid_path"),
            )

        old_content = target.read_text(encoding="utf-8", errors="replace")
        req = ApprovalRequest(
            tool_name="delete_file",
            rel_path=rel_path,
            old_content=old_content,
            new_content="",
            is_new_file=False,
        )
        decision = approval_cb(req)

        if decision.action == "reject":
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied(
                    {"ok": False, "error": "User rejected this deletion.", "path": rel_path, "rel_path": rel_path, "failure_class": "approval_rejected", "reason": reason},
                    "approval_rejected",
                ),
                extras={
                    "approval": "reject",
                    "rel_path": rel_path,
                    "approval_metadata": decision.metadata,
                },
            )
        if decision.action == "reject_all":
            return ToolExecResult(
                ok=False,
                payload=_mark_delete_not_applied(
                    {
                        "ok": False,
                        "error": "User rejected this deletion and all further writes in this turn.",
                        "path": rel_path,
                        "rel_path": rel_path,
                        "failure_class": "approval_rejected",
                        "reason": reason,
                    },
                    "approval_rejected",
                ),
                extras={
                    "approval": "reject_all",
                    "rel_path": rel_path,
                    "approval_metadata": decision.metadata,
                },
            )

        backup_path = _reg.backup_existing(self._root, target)
        target.unlink()

        rel_backup = (
            safe_relative_to(backup_path, self._root).as_posix() if backup_path is not None else None
        )
        return ToolExecResult(
            ok=True,
            payload={
                "ok": True,
                "applied": True,
                "path": rel_path,
                "rel_path": rel_path,
                "deleted": True,
                "write_outcome": "deleted",
                "applied_tool": "delete_file",
                "is_new_file": False,
                "backup": rel_backup,
                "backup_path": rel_backup,
                "reason": reason,
            },
            extras={
                "approval": "approve",
                "rel_path": rel_path,
                "approval_metadata": decision.metadata,
            },
        )

    def _handle_write(
        self,
        name: str,
        args: dict,
        approval_cb,
        reject_all: bool,
    ) -> ToolExecResult:
        if reject_all:
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": "User rejected all writes in this turn.", "failure_class": "approval_rejected"},
                    "approval_rejected",
                ),
                extras={"rejected_all": True},
            )

        path_arg = args.get("path", "")
        target = self._resolve_in_root(path_arg)
        if name == "write_file":
            rel_path = safe_relative_to(target, self._root).as_posix()
            if _is_validation_scratch_path(rel_path) and not _is_aura_tmp_scratch_path(rel_path):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({
                        "ok": False,
                        "path": rel_path,
                        "rel_path": rel_path,
                        "error": (
                            "Validation scratch files should use run_terminal_command "
                            "with python -c, or create and remove a temporary file "
                            "inside one terminal command."
                        ),
                        "failure_class": "validation_scratch_banned",
                        "suggested_next_tool": "run_terminal_command",
                        "suggested_next_action": (
                            "Use python -c for scratch validation, or create and remove "
                            "a temporary file inside one terminal command."
                        ),
                    }),
                )
            if _is_new_root_validation_scratch(self._root, target):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({
                        "ok": False,
                        "path": rel_path,
                        "rel_path": rel_path,
                        "error": "Root-level _check*.py validation scratch files are not allowed.",
                        "failure_class": "validation_scratch_banned",
                        "suggested_next_tool": "run_terminal_command",
                        "suggested_next_action": (
                            "Use python -c, an existing focused test, or .aura/tmp "
                            "with cleanup."
                        ),
                    }),
                )

        if name == "write_file":
            content = args.get("content", "")
            if not isinstance(content, str):
                return ToolExecResult(
                    ok=False, payload=_mark_not_applied({"ok": False, "error": "content must be a string", "failure_class": "internal_error"})
                )
            if _is_aura_tmp_scratch_path(rel_path):
                is_new_file = not target.exists()
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(target, content.encode("utf-8"))
                return ToolExecResult(
                    ok=True,
                    payload={
                        "ok": True,
                        "path": rel_path,
                        "applied": True,
                        "applied_tool": "write_file",
                        "write_outcome": "diagnostic_scratch_applied",
                        "is_new_file": is_new_file,
                        "diagnostic_scratch": True,
                    },
                )
            proposal = _reg.propose_write(self._root, target, content)
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=_mark_not_applied(proposal))

            # Humanizer: clean new Python file content before approval
            if proposal.get("is_new_file", False):
                gate_error = _maybe_humanize_proposal(proposal)
                if gate_error is not None:
                    return gate_error
            syntax_error = _python_syntax_error_payload(proposal)
            if syntax_error is not None:
                return ToolExecResult(ok=False, payload=syntax_error)

            craft_error = _run_craft_gate(
                proposal,
                "write_file",
                contract=self.get_contract(),
                workspace_root=self._root,
                task_shape=self.get_task_shape(),
            )
            if craft_error is not None:
                return craft_error

            req = ApprovalRequest(
                tool_name="write_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=proposal.get("is_new_file", False),
            )
        elif name == "apply_edit_transaction":
            operations = args.get("operations")
            expected_file_hash = args.get("expected_file_hash")
            description = args.get("description")
            if not isinstance(operations, list):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "operations must be a list", "failure_class": "edit_transaction_invalid_operation"}),
                )
            if expected_file_hash is not None and not isinstance(expected_file_hash, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "expected_file_hash must be a string when supplied", "failure_class": "edit_transaction_invalid_operation"}),
                )
            if description is not None and not isinstance(description, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "description must be a string when supplied", "failure_class": "edit_transaction_invalid_operation"}),
                )
            proposal = _reg.propose_edit_transaction(
                self._root,
                target,
                operations,
                expected_file_hash=expected_file_hash,
                description=description,
            )
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=_mark_not_applied(proposal))

            gate_error = _maybe_humanize_proposal(proposal)
            if gate_error is not None:
                return gate_error
            craft_error = _run_craft_gate(
                proposal,
                "apply_edit_transaction",
                contract=self.get_contract(),
                workspace_root=self._root,
                task_shape=self.get_task_shape(),
            )
            if craft_error is not None:
                return craft_error

            req = ApprovalRequest(
                tool_name="apply_edit_transaction",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
            )
        elif name == "edit_file":
            old_str = args.get("old_str", "")
            new_str = args.get("new_str", "")
            if not isinstance(old_str, str) or not isinstance(new_str, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "old_str and new_str must be strings", "failure_class": "internal_error"}),
                )
            proposal = _reg.propose_edit(self._root, target, old_str, new_str)
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=_mark_not_applied(proposal))
            syntax_error = _python_syntax_error_payload(proposal)
            if syntax_error is not None:
                return ToolExecResult(ok=False, payload=syntax_error)

            # Humanizer: observe-only for existing file edits
            _maybe_observe_humanizer(proposal)
            
            craft_error = _run_craft_gate(
                proposal,
                "edit_file",
                contract=self.get_contract(),
                workspace_root=self._root,
                task_shape=self.get_task_shape(),
            )
            if craft_error is not None:
                return craft_error

            req = ApprovalRequest(
                tool_name="edit_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
            )
        elif name == "edit_line_range":
            start_line = args.get("start_line")
            end_line = args.get("end_line")
            new_str = args.get("new_str", "")
            expected_old_str = args.get("expected_old_str")
            expected_old_hash = args.get("expected_old_hash")
            if not isinstance(start_line, int) or not isinstance(end_line, int) or not isinstance(new_str, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "start_line and end_line must be integers, new_str must be a string", "failure_class": "internal_error"}),
                )
            if expected_old_str is not None and not isinstance(expected_old_str, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "expected_old_str must be a string when supplied", "failure_class": "internal_error"}),
                )
            if expected_old_hash is not None and not isinstance(expected_old_hash, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "expected_old_hash must be a string when supplied", "failure_class": "internal_error"}),
                )
            proposal = _reg.propose_line_range_edit(
                self._root,
                target,
                start_line,
                end_line,
                new_str,
                expected_old_str=expected_old_str,
                expected_old_hash=expected_old_hash,
            )
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=_mark_not_applied(proposal))

            _maybe_observe_humanizer(proposal)
            craft_error = _run_craft_gate(
                proposal,
                "edit_line_range",
                contract=self.get_contract(),
                workspace_root=self._root,
                task_shape=self.get_task_shape(),
            )
            if craft_error is not None:
                return craft_error

            req = ApprovalRequest(
                tool_name="edit_line_range",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
            )
        elif name == "patch_file":
            edits = args.get("edits")
            expected_file_hash = args.get("expected_file_hash")
            description = args.get("description")
            if not isinstance(edits, list):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "edits must be a list", "failure_class": "internal_error"}),
                )
            if expected_file_hash is not None and not isinstance(expected_file_hash, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "expected_file_hash must be a string when supplied", "failure_class": "internal_error"}),
                )
            if description is not None and not isinstance(description, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "description must be a string when supplied", "failure_class": "internal_error"}),
                )
            proposal = _reg.propose_patch_file(
                self._root,
                target,
                edits,
                expected_file_hash=expected_file_hash,
                description=description,
            )
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=_mark_not_applied(proposal))

            _maybe_observe_humanizer(proposal)
            craft_error = _run_craft_gate(
                proposal,
                "patch_file",
                contract=self.get_contract(),
                workspace_root=self._root,
                task_shape=self.get_task_shape(),
            )
            if craft_error is not None:
                return craft_error

            req = ApprovalRequest(
                tool_name="patch_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
            )
        else:  # edit_symbol
            symbol_type = args.get("symbol_type", "")
            symbol_name = args.get("symbol_name", "")
            new_definition = args.get("new_definition", "")
            class_name = args.get("class_name")
            if not isinstance(symbol_type, str) or not isinstance(symbol_name, str) or not isinstance(new_definition, str):
                return ToolExecResult(
                    ok=False,
                    payload=_mark_not_applied({"ok": False, "error": "symbol_type, symbol_name, and new_definition must be strings", "failure_class": "internal_error"}),
                )
            proposal = _reg.propose_edit_symbol(
                self._root, target, symbol_type, symbol_name, new_definition, class_name
            )
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=_mark_not_applied(proposal))

            # Humanizer: behavior-changing for existing Python file edits
            gate_error = _maybe_humanize_proposal(proposal)
            if gate_error is not None:
                return gate_error
            craft_error = _run_craft_gate(
                proposal,
                "edit_symbol",
                contract=self.get_contract(),
                workspace_root=self._root,
                task_shape=self.get_task_shape(),
            )
            if craft_error is not None:
                return craft_error

            req = ApprovalRequest(
                tool_name="edit_symbol",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
            )

        decision = approval_cb(req)

        if decision.action == "reject":
            return ToolExecResult(
                ok=False,
                payload=_mark_not_applied(
                    {"ok": False, "error": "User rejected this change.", "path": req.rel_path, "failure_class": "approval_rejected"},
                    "approval_rejected",
                ),
                extras={
                    "approval": "reject",
                    "rel_path": req.rel_path,
                    "approval_metadata": decision.metadata,
                },
            )
        if decision.action == "reject_all":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": "User rejected this change and all further writes in this turn.",
                    "path": req.rel_path,
                    "failure_class": "approval_rejected",
                    "applied": False,
                    "write_outcome": "not_applied_user_rejected",
                },
                extras={
                    "approval": "reject_all",
                    "rel_path": req.rel_path,
                    "approval_metadata": decision.metadata,
                },
            )

        # Approve — back up if file exists, write new content.
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_path = _reg.backup_existing(self._root, target)
        _atomic_write_bytes(target, req.new_content.encode("utf-8"))

        rel_backup = (
            safe_relative_to(backup_path, self._root).as_posix() if backup_path is not None else None
        )
        payload = {
            "ok": True,
            "path": req.rel_path,
            "applied": True,
            "applied_tool": name,
            "write_outcome": proposal.get("write_outcome") or "applied",
            "is_new_file": req.is_new_file,
            "backup": rel_backup,
        }
        if proposal.get("pre_existing_environment_issues"):
            payload["pre_existing_environment_issues"] = proposal.get("pre_existing_environment_issues")
        introduced_environment_issues = None
        if isinstance(proposal.get("craft_metadata"), dict):
            introduced_environment_issues = proposal["craft_metadata"].get("introduced_environment_issues")
        if introduced_environment_issues:
            payload["introduced_environment_issues"] = introduced_environment_issues
        if proposal.get("checks_warned"):
            payload["checks_warned"] = proposal.get("checks_warned")
        if proposal.get("craft_warnings"):
            payload["craft_warnings"] = proposal.get("craft_warnings")
        if proposal.get("craft_metadata"):
            payload["craft_metadata"] = proposal.get("craft_metadata")
        if name == "edit_line_range":
            payload["start_line"] = proposal.get("start_line")
            payload["end_line"] = proposal.get("end_line")
        if name == "patch_file":
            payload["hunk_count"] = proposal.get("hunk_count", 0)
        if name == "apply_edit_transaction":
            payload["operation_count"] = proposal.get("operation_count", 0)
        return ToolExecResult(
            ok=True,
            payload=payload,
            extras={
                "approval": "approve",
                "rel_path": req.rel_path,
                "approval_metadata": decision.metadata,
            },
        )
