"""Completion message and caveat construction helpers for bridge dispatch."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from aura.bridge.event_relay import WorkerEventRelay
from aura.conversation import WorkerDispatchRequest
from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
)
from aura.conversation.path_utils import (
    normalize_worker_path as _normalize_worker_path,
)
from aura.conversation.worker_completion._summary_formatters import (
    _final_report_claims_failure,
    _format_recoverable_write_failure,
    _format_structured_worker_failure,
    _format_worker_write_failure,
    _parse_structured_worker_failure,
)

_log = logging.getLogger(__name__)

__all__ = [
    "_build_worker_completion_messages",
    "_check_read_before_edit",
    "_is_recoverable_worker_write_failure",
    "_diagnostic_environment_caveats",
]

RECOVERABLE_WORKER_WRITE_FAILURE_CLASSES = {
    "edit_mechanics_symbol_not_found",
    "edit_mechanics_old_str_not_found",
    "edit_mechanics_ambiguous_match",
    "edit_mechanics_stale_line_range",
    "edit_mechanics_multi_edit_spin",
    "patch_hunk_not_found",
    "patch_hunk_ambiguous",
    "patch_file_hash_mismatch",
    "syntax_invalid",
}


def _build_worker_completion_messages(
    *,
    req: WorkerDispatchRequest,
    relay: WorkerEventRelay,
    completion: dict[str, Any],
    internal_error: str | None,
    cleaned_scratch_files: list[str],
    workspace_root: Path | None,
) -> dict[str, Any]:
    result_errors = list(relay.api_errors)
    if internal_error:
        result_errors.insert(0, "Harness error due to an internal Worker exception.")

    final_report = completion["final_report"]
    continuation = completion["continuation"]
    write_failures = completion["write_failures"]
    source_inspection_blockers = completion["source_inspection_blockers"]
    terminal_policy_blockers = completion["terminal_policy_blockers"]
    environment_setup_blockers = completion["environment_setup_blockers"]
    failed_validation = completion["failed_validation"]
    validation_not_run = completion["validation_not_run"]
    validation_command_issues = completion["validation_command_issues"]
    diagnostic_environment_caveats = completion["diagnostic_environment_caveats"]
    acceptance_unverified = completion["acceptance_unverified"]
    is_implementation = completion["is_implementation"]

    structured_failure = _parse_structured_worker_failure(final_report)
    if structured_failure:
        if structured_failure.get("status") == "mismatch_detected":
            if not continuation.get("mismatch"):
                continuation["mismatch"] = structured_failure.get("mismatch")
            continuation["status"] = "harness_error"
            continuation["reason"] = "worker_mismatch"
        else:
            result_errors.append(_format_structured_worker_failure(structured_failure))

    recoverable_write_failures = [
        r for r in write_failures if _is_recoverable_worker_write_failure(r)
    ]
    failed_write_tools = [
        r for r in write_failures if not _is_recoverable_worker_write_failure(r)
    ]
    for r in failed_write_tools:
        result_errors.append(_format_worker_write_failure(r))

    if not structured_failure:
        for r in source_inspection_blockers:
            command = str(r.get("blocked_command") or "")[:120]
            suffix = f": {command}" if command else "."
            result_errors.append(
                "Terminal source inspection was blocked; Worker should retry with structured reads"
                + suffix
            )
        for r in terminal_policy_blockers:
            if r.get("failure_class") == "source_inspection_command_blocked":
                continue
            command = str(r.get("blocked_command") or "")[:120]
            suffix = f": {command}" if command else "."
            result_errors.append(
                "Worker terminal command was blocked because it was not validation/build/test"
                + suffix
            )
        for r in environment_setup_blockers:
            dependency = str(r.get("missing_dependency") or r.get("missing_tool") or "tool/dependency")
            command = str(r.get("blocked_command") or "")[:120]
            suffix = f": {command}" if command else "."
            label = "dependency" if r.get("missing_dependency") else "tool"
            result_errors.append(
                f"Project environment missing {label} '{dependency}'"
                + suffix
            )
        for v in failed_validation:
            cmd = v["command"][:80]
            result_errors.append(f"Validation command failed (exit code {v['exit_code']}): {cmd}")

    if workspace_root is None:
        edited_without_read = _check_read_before_edit(
            relay.read_files,
            relay.read_outline_files,
            relay.edited_existing_files,
        )
    else:
        edited_without_read = _check_read_before_edit(
            relay.read_files,
            relay.read_outline_files,
            relay.edited_existing_files,
            file_exists=_workspace_file_exists(workspace_root),
        )
    if edited_without_read:
        result_errors.append(
            "Worker edited existing file(s) without reading them first: "
            + ", ".join(edited_without_read[:5])
        )

    result_caveats: list[str] = []
    for write in relay.write_results:
        issues = write.get("pre_existing_environment_issues")
        if isinstance(issues, list) and issues:
            first = issues[0]
            if isinstance(first, dict):
                msg = str(first.get("message") or first.get("code") or "pre-existing environment issue")
            else:
                msg = str(first)
            result_caveats.append(f"Pre-existing environment issue on {write.get('path')}: {msg}")

    if recoverable_write_failures and not relay.write_results and not structured_failure:
        result_caveats.append(_format_recoverable_write_failure(recoverable_write_failures[0]))
    if validation_not_run:
        result_caveats.append("Files changed but validation did not run.")
    if validation_command_issues:
        result_caveats.append("Validation command issue(s) were recorded; code validation failures are reported separately.")    # ── Required behavioral validation enforcement ────────────────
    behavioral = completion.get("behavioral_validation", {})
    behavioral_skipped = behavioral.get("skipped", [])
    behavioral_could_not_run = behavioral.get("could_not_run", [])
    if behavioral_skipped:
        skipped_str = ", ".join(behavioral_skipped[:5])
        result_errors.append(
            f"Required behavioral validation skipped: {skipped_str}"
        )
    for entry in behavioral_could_not_run:
        cmd = entry.get("command", "")
        reason = entry.get("reason", "")
        caveat = f"Required behavioral check could not run: {cmd}"
        if reason:
            caveat += f" ({reason})"
        if caveat not in result_caveats:
            result_caveats.append(caveat)
    for caveat in diagnostic_environment_caveats:
        if caveat not in result_caveats:
            result_caveats.append(caveat)

    if cleaned_scratch_files:
        result_caveats.append(
            "Cleaned Worker-created root validation scratch file(s): "
            + ", ".join(cleaned_scratch_files[:5])
        )

    if acceptance_unverified:
        has_deterministic_proof = (
            completion.get("has_writes")
            and bool(completion.get("validation_results"))
            and not completion.get("failed_validation")
        )
        if not has_deterministic_proof:
            result_caveats.append("Worker final report did not clearly mention validation or acceptance verification.")

    if not structured_failure and _final_report_claims_failure(final_report):
        phrase_caveat = (
            "Worker final report mentioned possible blocker, failed validation, "
            "or incomplete verification."
        )
        result_caveats.append(phrase_caveat)

    quality_findings: list[Any] = []
    if workspace_root is not None and completion["has_writes"] and relay.touched_files:
        try:
            from aura.code_intel.audit import audit_changed_files

            touched = sorted(relay.touched_files)
            audit_findings = audit_changed_files(workspace_root, touched)
            warning_findings = [f for f in audit_findings if f.severity == "warning"]
            quality_findings.extend(warning_findings)
            blocked = [f for f in audit_findings if f.severity in ("error",)]
            if warning_findings:
                warning_files = sorted({f.file for f in warning_findings})
                result_caveats.append(
                    "Post-edit structural audit reported warning-level findings in: "
                    + ", ".join(warning_files[:5])
                    + "."
                )
            if blocked:
                failure_files = sorted({f.file for f in blocked})
                msg = (
                    "Post-edit structural audit found high-severity issues in: "
                    + ", ".join(failure_files[:5])
                    + ". Fix these before declaring success."
                )
                result_errors.append(msg)
                for bf in blocked[:5]:
                    result_errors.append(f"  {bf.file}:{bf.line}: {bf.message}")
        except Exception:
            _log.exception("Post-edit structural audit failed")

    no_failure_or_blocker = (
        not relay.failed_tool_results
        and not internal_error
        and not relay.api_errors
    )
    if (
        is_implementation
        and not structured_failure
        and not relay.touched_files
        and no_failure_or_blocker
    ):
        if completion["validation_results"] and not failed_validation:
            result_errors.append(
                "Harness no-progress: Worker ran validation but made no implementation changes."
            )
        elif not completion["validation_results"]:
            result_errors.append(
                "Harness no-progress: Worker made no changes, reported no blocker, "
                "and ran no meaningful validation."
            )

    return {
        "structured_failure": structured_failure,
        "recoverable_write_failures": recoverable_write_failures,
        "failed_write_tools": failed_write_tools,
        "result_errors": result_errors,
        "result_caveats": result_caveats,
        "quality_findings": _quality_findings_to_metadata(quality_findings),
    }


def _is_recoverable_worker_write_failure(result: dict[str, Any]) -> bool:
    if result.get("internal_recovery_steer"):
        return True
    failure_class = str(result.get("failure_class") or "")
    if failure_class == "syntax_invalid" and result.get("recoverable") is False:
        return False
    return failure_class in RECOVERABLE_WORKER_WRITE_FAILURE_CLASSES


def _check_read_before_edit(
    read_files: set[str],
    read_outline_files: set[str],
    edited_existing_files: list[str],
    *,
    file_exists: Any = None,
) -> list[str]:
    """Return paths of existing files that were edited without being read."""
    if file_exists is None:
        file_exists = lambda p: Path(p).exists()  # noqa: E731
    all_read = {
        _normalize_worker_path(path)
        for path in (set(read_files) | set(read_outline_files))
    }
    return [
        p for p in edited_existing_files
        if _normalize_worker_path(p) not in all_read
        and file_exists(_normalize_worker_path(p))
    ]


def _workspace_file_exists(workspace_root: Path):
    root = Path(workspace_root).resolve()

    def exists(path: str) -> bool:
        try:
            candidate = Path(_normalize_worker_path(path))
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return False
        return resolved.exists()

    return exists


def _diagnostic_environment_caveats(relay: Any) -> list[str]:
    dependencies: list[str] = []
    records: list[dict[str, Any]] = []
    for attr in ("not_applied_writes", "failed_tool_results"):
        value = getattr(relay, attr, [])
        if isinstance(value, list):
            records.extend(item for item in value if isinstance(item, dict))

    for record in records:
        path = str(record.get("path") or record.get("rel_path") or "")
        if not _is_validation_scratch_path(path):
            continue
        _collect_missing_dependencies(record, dependencies)

    for result in getattr(relay, "terminal_results", []):
        if not isinstance(result, dict) or result.get("ok"):
            continue
        command = str(result.get("command") or "")
        if not _command_mentions_scratch_path(command):
            continue
        _collect_missing_dependencies(result, dependencies)

    caveats: list[str] = []
    for dependency in dependencies:
        caveat = (
            "Diagnostic script could not run because "
            f"{dependency} is not installed in the project environment."
        )
        if caveat not in caveats:
            caveats.append(caveat)
    return caveats


def _quality_findings_to_metadata(findings: Any) -> list[dict[str, Any]]:
    if not isinstance(findings, list):
        return []
    metadata: list[dict[str, Any]] = []
    for finding in findings:
        metadata.append(
            {
                "severity": str(getattr(finding, "severity", "")),
                "kind": str(getattr(finding, "kind", "audit")),
                "file": str(getattr(finding, "file", "")),
                "line": getattr(finding, "line", None),
                "message": str(getattr(finding, "message", "")),
            }
        )
    return metadata


def _collect_missing_dependencies(record: dict[str, Any], dependencies: list[str]) -> None:
    explicit = record.get("missing_dependency")
    if isinstance(explicit, str) and explicit:
        _append_unique(dependencies, explicit)

    for key in ("introduced_environment_issues", "craft_issues"):
        issues = record.get(key)
        if not isinstance(issues, list):
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            dependency = _dependency_from_text(str(issue.get("message") or ""))
            if dependency:
                _append_unique(dependencies, dependency)

    for key in ("error", "result_preview", "output", "output_preview"):
        dependency = _dependency_from_text(str(record.get(key) or ""))
        if dependency:
            _append_unique(dependencies, dependency)


def _dependency_from_text(text: str) -> str | None:
    patterns = (
        r"Import source '([^']+)' could not be resolved",
        r'Import source "([^"]+)" could not be resolved',
        r"No module named '([^']+)'",
        r'No module named "([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).split(".", 1)[0]
    return None


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _command_mentions_scratch_path(command: str) -> bool:
    normalized = _normalize_worker_path(command)
    for token in re.split(r"\s+", normalized):
        token = token.strip("'\"")
        if _is_validation_scratch_path(token):
            return True
    return False
