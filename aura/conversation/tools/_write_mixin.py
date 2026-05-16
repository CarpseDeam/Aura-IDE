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
from pathlib import Path

from aura.conversation.tools._types import ApprovalRequest, ToolExecResult

# Import the registry module so we can look up functions at call time.
# This creates a circular import, but Python handles it because
# `registry` is already in sys.modules by the time this module is loaded.
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
    if not _humanizer_enabled():
        return False
    if _humanizer_observe_enabled():
        return False

    settings = _humanizer_settings()
    enabled = True if settings is None else bool(getattr(settings, "humanizer_gate_enabled", True))

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
        payload={
            "ok": False,
            "error": "Aura humanizer rejected generated Python before approval.",
            "humanizer_gate": True,
            "path": rel_path,
            "slop_score": getattr(report, "score", 0.0) if report else 0.0,
            "slop_status": getattr(report, "status", "unknown") if report else "unknown",
            "issue_count": getattr(report, "issue_count", 0) if report else 0,
            "blocking_issue_count": len(blocking_issues),
            "issues": issues_payload,
        },
    )


def _maybe_observe_humanizer(proposal: dict) -> None:
    """Run humanizer in observe-only mode for existing .py file edits."""
    if not _humanizer_enabled():
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


class WriteHandlersMixin:
    """Handlers for write tools — guards + approval + backup."""

    def _handle_write_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled."})
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                },
            )
        return self._handle_write("write_file", args, approval_cb, reject_all)

    def _handle_edit_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled."})
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                },
            )
        return self._handle_write("edit_file", args, approval_cb, reject_all)

    def _handle_edit_symbol(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled."})
        if self._mode == "planner":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": (
                        "Planner cannot write directly. "
                        "You must use the 'dispatch_to_worker' tool to specify code changes. "
                        "Include your intended edits in the 'spec' field of the dispatch."
                    ),
                },
            )
        return self._handle_write("edit_symbol", args, approval_cb, reject_all)

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
                payload={"ok": False, "error": "User rejected all writes in this turn."},
                extras={"rejected_all": True},
            )

        path_arg = args.get("path", "")
        target = self._resolve_in_root(path_arg)

        if name == "write_file":
            content = args.get("content", "")
            if not isinstance(content, str):
                return ToolExecResult(
                    ok=False, payload={"ok": False, "error": "content must be a string"}
                )
            proposal = _reg.propose_write(self._root, target, content)
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=proposal)

            # Humanizer: clean new Python file content before approval
            if proposal.get("is_new_file", False):
                gate_error = _maybe_humanize_proposal(proposal)
                if gate_error is not None:
                    return gate_error

            req = ApprovalRequest(
                tool_name="write_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=proposal.get("is_new_file", False),
            )
        elif name == "edit_file":
            old_str = args.get("old_str", "")
            new_str = args.get("new_str", "")
            if not isinstance(old_str, str) or not isinstance(new_str, str):
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "old_str and new_str must be strings"},
                )
            proposal = _reg.propose_edit(self._root, target, old_str, new_str)
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=proposal)

            # Humanizer: observe-only for existing file edits, gated by env var
            if os.environ.get("AURA_HUMANIZER_EDIT_FILE", "") == "1":
                _maybe_observe_humanizer(proposal)

            req = ApprovalRequest(
                tool_name="edit_file",
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
                    payload={"ok": False, "error": "symbol_type, symbol_name, and new_definition must be strings"},
                )
            proposal = _reg.propose_edit_symbol(
                self._root, target, symbol_type, symbol_name, new_definition, class_name
            )
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=proposal)

            # Humanizer: behavior-changing for existing Python file edits
            gate_error = _maybe_humanize_proposal(proposal)
            if gate_error is not None:
                return gate_error

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
                payload={"ok": False, "error": "User rejected this change."},
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
        target.write_text(req.new_content, encoding="utf-8")
        rel_backup = (
            backup_path.relative_to(self._root).as_posix() if backup_path is not None else None
        )
        return ToolExecResult(
            ok=True,
            payload={
                "ok": True,
                "path": req.rel_path,
                "applied": name,
                "is_new_file": req.is_new_file,
                "backup": rel_backup,
            },
            extras={
                "approval": "approve",
                "rel_path": req.rel_path,
                "approval_metadata": decision.metadata,
            },
        )
