from __future__ import annotations

from pathlib import Path

import pytest

from aura.conversation.worker_quality import (
    LARGE_DIFF_LINE_THRESHOLD,
    evaluate_worker_quality,
)


@pytest.fixture(autouse=True)
def _disable_code_intel_audit(monkeypatch):
    monkeypatch.setattr(
        "aura.conversation.quality.evaluator.audit_changed_files",
        lambda workspace_root, changed_files: [],
    )


def test_evaluate_worker_quality_flags_debug_temp_production_code(tmp_path: Path):
    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/service.py"],
        _diff("aura/service.py", ['print("debug")']),
        validation_passed=True,
    )

    assert _has_finding(decision, "temporary_production_code", "error")


def test_evaluate_worker_quality_does_not_flag_normal_diagnostic_wording(
    tmp_path: Path,
):
    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/service.py"],
        _diff("aura/service.py", [
            'message = "Diagnostic command completed"',
            'summary = "Collected diagnostics for the failed run"',
        ]),
        validation_passed=True,
    )

    assert not _has_finding(decision, "temporary_production_code")


def test_evaluate_worker_quality_flags_explicit_temp_probe_markers(tmp_path: Path):
    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/service.py"],
        _diff("aura/service.py", [
            "# DIAGNOSTIC: temporary state dump",
            "# debug probe for worker activity",
            "# event probe marker",
            "# TODO: remove before release",
        ]),
        validation_passed=True,
    )

    findings = [
        finding
        for finding in decision.findings
        if finding.kind == "temporary_production_code"
    ]
    markers = {finding.evidence["marker"] for finding in findings}
    assert {"DIAGNOSTIC", "debug probe", "event probe", "TODO: remove"} <= markers


def test_evaluate_worker_quality_ignores_markers_in_tests_and_docs(tmp_path: Path):
    diff_text = "\n".join([
        _diff("tests/test_service.py", ['print("debug")']),
        _diff("docs/notes.md", ["HACK: explain temporary note"]),
    ])

    decision = evaluate_worker_quality(
        tmp_path,
        ["tests/test_service.py", "docs/notes.md"],
        diff_text,
        validation_passed=True,
    )

    assert decision.findings == []


def test_evaluate_worker_quality_flags_unexpected_production_files(tmp_path: Path):
    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/expected.py", "aura/unexpected.py"],
        "",
        validation_passed=True,
        expected_files=["aura/expected.py"],
    )

    assert _has_finding(decision, "unexpected_production_file", "error")


def test_evaluate_worker_quality_allows_expected_production_files(tmp_path: Path):
    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/expected.py"],
        "",
        validation_passed=True,
        expected_files=["/aura\\expected.py"],
    )

    assert not _has_finding(decision, "unexpected_production_file")


def test_evaluate_worker_quality_flags_placeholder_production_code(tmp_path: Path):
    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/service.py"],
        _diff("aura/service.py", ["raise NotImplementedError"]),
        validation_passed=True,
    )

    assert _has_finding(decision, "placeholder_production_code", "error")


def test_evaluate_worker_quality_flags_broad_exception_swallowing(tmp_path: Path):
    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/service.py"],
        _diff("aura/service.py", ["try:", "    run()", "except Exception:", "    pass"]),
        validation_passed=True,
    )

    assert _has_finding(decision, "swallowed_exception", "error")


def test_evaluate_worker_quality_preserves_duplicate_string_warning(tmp_path: Path):
    literal = "same production message text"
    diff_text = "\n".join([
        _diff("aura/one.py", [f'MESSAGE = "{literal}"']),
        _diff("aura/two.py", [f'MESSAGE = "{literal}"']),
    ])

    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/one.py", "aura/two.py"],
        diff_text,
        validation_passed=True,
    )

    assert _has_finding(decision, "duplicate_changed_string", "warning")


def test_evaluate_worker_quality_preserves_large_diff_warning(tmp_path: Path):
    added = [f"value_{index} = {index}" for index in range(LARGE_DIFF_LINE_THRESHOLD + 1)]

    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/service.py"],
        _diff("aura/service.py", added),
        validation_passed=True,
    )

    assert _has_finding(decision, "large_diff_whole_file_rewrite", "warning")


def test_evaluate_worker_quality_preserves_protected_control_flow_warning(tmp_path: Path):
    decision = evaluate_worker_quality(
        tmp_path,
        ["aura/conversation/manager.py"],
        _diff("aura/conversation/manager.py", ["if changed:", "    return True"]),
        validation_passed=True,
    )

    assert _has_finding(decision, "protected_file_controlflow", "warning")


def _diff(path: str, added_lines: list[str]) -> str:
    body = "\n".join(f"+{line}" for line in added_lines)
    return "\n".join([
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,1 +1,{max(1, len(added_lines))} @@",
        body,
    ])


def _has_finding(decision, kind: str, severity: str | None = None) -> bool:
    return any(
        finding.kind == kind and (severity is None or finding.severity == severity)
        for finding in decision.findings
    )
