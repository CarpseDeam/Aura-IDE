"""Tests for aura/conversation/validation_failure_routing.py."""
from __future__ import annotations

import re

from aura.conversation.validation_failure_routing import (
    ValidationFailureVerdict,
    _compute_digest,
    _normalize_diagnostics,
    route_validation_failure,
    validation_diagnostics_preview,
)
from aura.conversation.validation_orchestrator import (
    ENVIRONMENT_ERROR,
    PRODUCT_VALIDATION_FAILED,
    ValidationRunResult,
)
from aura.conversation.worker_final_validation import WorkerFinalValidationResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    *,
    ok: bool = False,
    command: str = "pytest",
    diagnostics: str = "AssertionError: expected 3 got 5",
    infra: bool = False,
) -> WorkerFinalValidationResult:
    """Build a WorkerFinalValidationResult with one failing run."""
    classification = ENVIRONMENT_ERROR if infra else PRODUCT_VALIDATION_FAILED
    run = ValidationRunResult(
        command=command,
        raw_text=command,
        exit_code=1,
        output=diagnostics,
        classification=classification,
        counts_as_product_failure=not infra,
    )
    return WorkerFinalValidationResult(
        ok=ok,
        diagnostics=diagnostics,
        command=command,
        runs=[run],
    )


def _make_stalled_state(
    fingerprint: str,
    command_key: str = "pytest",
) -> dict[str, str]:
    return {command_key: fingerprint}


def _fingerprint_for(
    result: WorkerFinalValidationResult,
    *,
    command_key: str | None = None,
) -> str:
    """Compute the fingerprint that ``route_validation_failure`` would produce
    for *result*, using its internal helpers."""
    failing_runs = [r for r in (result.runs or []) if not r.ok]
    key = command_key or result.command or "|".join(
        r.command for r in failing_runs if r.command
    ) or "<unknown>"
    classifications = ",".join(r.classification for r in failing_runs) if failing_runs else result.diagnostics
    diag_parts = [r.output for r in failing_runs if r.output.strip()]
    if not diag_parts and result.diagnostics.strip():
        diag_parts.append(result.diagnostics)
    digest = _compute_digest("\n".join(diag_parts))
    return f"{key}|{classifications}|{digest}"


# ---------------------------------------------------------------------------
# Progress — first encounter
# ---------------------------------------------------------------------------

class TestProgressFirstEncounter:
    """When no fingerprint is stored yet, the router sees progress."""

    def test_infra_only_returns_fix_command(self):
        result = _make_result(infra=True)
        verdict = route_validation_failure(result, {}, False)
        assert verdict.action == "fix_command"
        assert VERDICT_INSTRUCTION_NONEMPTY(verdict)
        assert verdict.handback_details == {}

    def test_product_failure_returns_repair(self):
        result = _make_result(infra=False)
        verdict = route_validation_failure(result, {}, False)
        assert verdict.action == "repair"
        assert VERDICT_INSTRUCTION_NONEMPTY(verdict)
        assert verdict.handback_details == {}

    def test_stores_fingerprint_in_memory(self):
        mem: dict[str, str] = {}
        result = _make_result(infra=False)
        route_validation_failure(result, mem, False)
        assert "pytest" in mem
        assert len(mem["pytest"]) > 0


# ---------------------------------------------------------------------------
# Progress — changed fingerprint or intervening edits
# ---------------------------------------------------------------------------

class TestProgressChanged:
    """When the fingerprint differs from the stored one, the router continues."""

    def test_different_fingerprint_returns_repair(self):
        result = _make_result(diagnostics="New error: file not found")
        fp = _fingerprint_for(result)
        # Stored fingerprint is for a *different* diagnostic
        old_result = _make_result(diagnostics="Old error: timeout")
        old_fp = _fingerprint_for(old_result)
        assert fp != old_fp

        mem = {result.command: old_fp}
        verdict = route_validation_failure(result, mem, False)
        assert verdict.action == "repair"
        # Memory should now hold the new fingerprint
        assert mem[result.command] == fp

    def test_edits_since_last_pass_breaks_stall(self):
        result = _make_result(diagnostics="Same error every time")
        fp = _fingerprint_for(result)
        mem = {result.command: fp}
        # Fingerprint matches, but edits occurred → progress
        verdict = route_validation_failure(result, mem, True)
        assert verdict.action in ("repair", "fix_command")


# ---------------------------------------------------------------------------
# Stall — identical fingerprint, no intervening edit
# ---------------------------------------------------------------------------

class TestStallDetection:
    """When the fingerprint is identical and no edits occurred, handback."""

    def test_same_fingerprint_no_edits_returns_handback(self):
        result = _make_result(diagnostics="error: cannot find package")
        fp = _fingerprint_for(result)
        mem = {result.command: fp}
        verdict = route_validation_failure(result, mem, False)
        assert verdict.action == "handback"
        assert verdict.instruction == ""
        assert "failure_class" in verdict.handback_details
        assert verdict.handback_details["failure_class"] == "product_validation_failed"
        assert "details" in verdict.handback_details

    def test_handback_details_contain_command_and_diagnostics_preview(self):
        result = _make_result(
            command="pytest tests/",
            diagnostics="FAILED test_foo.py::test_bar - AssertionError",
        )
        fp = _fingerprint_for(result)
        mem = {result.command: fp}
        verdict = route_validation_failure(result, mem, False)
        details = verdict.handback_details["details"]
        assert details["command"] == "pytest tests/"
        assert "AssertionError" in details["diagnostics_preview"]
        assert details["diagnostics_truncated"] is False
        assert details["diagnostics_char_count"] == len(
            "FAILED test_foo.py::test_bar - AssertionError"
        )

    def test_handback_details_suggest_redispatch(self):
        result = _make_result()
        fp = _fingerprint_for(result)
        mem = {result.command: fp}
        verdict = route_validation_failure(result, mem, False)
        action = verdict.handback_details["details"]["suggested_next_action"]
        assert "Redispatch" in action
        assert "dispatch_mismatch" in verdict.handback_details["details"]


# ---------------------------------------------------------------------------
# infra_only property integration
# ---------------------------------------------------------------------------

class TestInfraOnly:
    """The router correctly dispatches based on infra_only."""

    def test_infra_only_mixed_some_product_failure_is_not_infra_only(self):
        """If any run is a product failure, infra_only is false."""
        infra_run = ValidationRunResult(
            command="pytest", raw_text="pytest", exit_code=1,
            classification=ENVIRONMENT_ERROR, counts_as_product_failure=False,
            output="ModuleNotFoundError: nox",
        )
        product_run = ValidationRunResult(
            command="pytest", raw_text="pytest", exit_code=1,
            classification=PRODUCT_VALIDATION_FAILED, counts_as_product_failure=True,
            output="AssertionError: expected 4 got 5",
        )
        result = WorkerFinalValidationResult(
            ok=False, diagnostics="Multiple failures", command="pytest",
            runs=[infra_run, product_run],
        )
        mem: dict[str, str] = {}
        verdict = route_validation_failure(result, mem, False)
        assert verdict.action == "repair", (
            "mixed infra+product should repair, not fix_command"
        )

    def test_infra_only_instruction_text(self):
        result = _make_result(infra=True, command="cd frontend && npm test")
        mem: dict[str, str] = {}
        verdict = route_validation_failure(result, mem, False)
        assert "Do not edit product code" in verdict.instruction
        assert "cd frontend && npm test" in verdict.instruction

    def test_repair_instruction_contains_lint_guard(self):
        result = _make_result(infra=False)
        mem: dict[str, str] = {}
        verdict = route_validation_failure(result, mem, False)
        assert "noqa: F401" in verdict.instruction
        assert "compatibility re-export" in verdict.instruction


# ---------------------------------------------------------------------------
# Multiple failing runs
# ---------------------------------------------------------------------------

class TestMultiRun:
    """Fingerprint is built from all failing runs, not just the first."""

    def test_multiple_failing_runs_included_in_classifications(self):
        run_a = ValidationRunResult(
            command="pytest a_test.py", raw_text="pytest a_test.py", exit_code=1,
            classification=PRODUCT_VALIDATION_FAILED, counts_as_product_failure=True,
            output="FAIL a_test.py",
        )
        run_b = ValidationRunResult(
            command="pytest b_test.py", raw_text="pytest b_test.py", exit_code=1,
            classification=ENVIRONMENT_ERROR, counts_as_product_failure=False,
            output="ModuleNotFoundError",
        )
        result = WorkerFinalValidationResult(
            ok=False, diagnostics="fail", command="pytest",
            runs=[run_a, run_b],
        )
        fp = _fingerprint_for(result)
        assert "product_validation_failed" in fp
        assert "environment_error" in fp


# ---------------------------------------------------------------------------
# Fingerprint stability
# ---------------------------------------------------------------------------

class TestFingerprintStability:
    """SHA-256 digests are deterministic and do not vary by process."""

    def test_deterministic_digest(self):
        text = "AssertionError in test_foo.py line 42"
        d1 = _compute_digest(text)
        d2 = _compute_digest(text)
        assert d1 == d2
        assert len(d1) == 16  # hexdigest()[:16]

    def test_not_builtin_hash(self):
        """Confirm we're not accidentally using hash() (randomized per process)."""
        text = "some error output"
        digest = _compute_digest(text)
        # Built-in hash() returns varying 64-bit ints; our digest is fixed-length hex
        assert re.fullmatch(r"[0-9a-f]{16}", digest), f"unexpected digest format: {digest}"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalization:
    """Diagnostic normalization strips noise so accidental changes don't
    disguise a replay."""

    def test_strips_windows_paths(self):
        noisy = "Error in C:\\Users\\me\\project\\src\\main.py line 42"
        clean = _normalize_diagnostics(noisy)
        assert "<path>" in clean
        assert "C:" not in clean or "<path>" in clean

    def test_strips_posix_paths(self):
        noisy = "Error in /home/me/project/src/main.py line 42"
        clean = _normalize_diagnostics(noisy)
        assert "<path>" in clean
        assert "/home/me" not in clean

    def test_strips_timestamps(self):
        noisy = "2026-07-07 14:30:00,123 ERROR something"
        clean = _normalize_diagnostics(noisy)
        assert "<timestamp>" in clean

    def test_collapses_whitespace(self):
        noisy = "error:   unexpected    spaces"
        clean = _normalize_diagnostics(noisy)
        assert "  " not in clean
        assert clean == "error: unexpected spaces"

    def test_normalized_diagnostics_produce_same_fingerprint(self):
        """Diagnostics differing only in paths/timestamps should yield the
        same fingerprint, triggering stall detection."""
        diag_a = "Error: file C:\\project\\src\\mod.py line 10 at 2026-07-07 12:00:00"
        diag_b = "Error: file /home/user/src/mod.py line 10 at 2026-07-07 12:00:01"

        result_a = _make_result(diagnostics=diag_a)
        result_b = _make_result(diagnostics=diag_b)

        fp_a = _fingerprint_for(result_a)
        fp_b = _fingerprint_for(result_b)
        assert fp_a == fp_b, (
            "normalized fingerprints should match despite different paths"
        )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases around empty runs, empty diagnostics, etc."""

    def test_runs_is_none(self):
        """A result with no runs list should not crash."""
        result = WorkerFinalValidationResult(ok=False, command="pytest", diagnostics="fail")
        mem: dict[str, str] = {}
        verdict = route_validation_failure(result, mem, False)
        assert verdict.action in ("repair", "fix_command", "handback")

    def test_runs_is_none_cold_start_returns_fix_command(self):
        """runs=None, first encounter → progress → fix_command (since no
        failing runs means infra_only is False so repair)."""
        result = WorkerFinalValidationResult(ok=False, command="pytest", diagnostics="fail")
        mem: dict[str, str] = {}
        verdict = route_validation_failure(result, mem, False)
        assert verdict.action == "repair"  # not infra_only because no failing runs

    def test_unknown_command_key_when_command_empty(self):
        """When both val_result.command and all run.commands are empty, the
        fallback key '<unknown>' is used."""
        run = ValidationRunResult(
            command="", raw_text="", exit_code=1,
            classification=ENVIRONMENT_ERROR, counts_as_product_failure=False,
            output="something went wrong",
        )
        result = WorkerFinalValidationResult(
            ok=False, diagnostics="fail", command="", runs=[run],
        )
        mem: dict[str, str] = {}
        verdict = route_validation_failure(result, mem, False)
        assert verdict.action == "fix_command"
        assert "<unknown>" in mem  # key was stored under the fallback


# ---------------------------------------------------------------------------
# validation_diagnostics_preview unit tests
# ---------------------------------------------------------------------------

class TestValidationDiagnosticsPreview:
    """``validation_diagnostics_preview`` bounds huge diagnostics without
    hiding the failure."""

    def test_huge_diagnostics_produce_preview(self):
        """Very long diagnostics produce a bounded preview."""
        huge = "X" * 10000
        result = validation_diagnostics_preview(huge, limit=2000)
        assert len(result["diagnostics_preview"]) == 2000
        assert result["diagnostics_preview"] == "X" * 2000

    def test_huge_diagnostics_set_truncated_true(self):
        huge = "X" * 10000
        result = validation_diagnostics_preview(huge, limit=2000)
        assert result["diagnostics_truncated"] is True

    def test_huge_diagnostics_include_char_count(self):
        huge = "X" * 10000
        result = validation_diagnostics_preview(huge, limit=2000)
        assert result["diagnostics_char_count"] == 10000

    def test_full_raw_diagnostics_not_in_handback_details(self):
        """Stall-path handback details must NOT contain the raw full
        diagnostics string — only bounded preview fields."""
        huge = "Y" * 50000
        result = _make_result(diagnostics=huge)
        fp = _fingerprint_for(result)
        mem = {result.command: fp}
        verdict = route_validation_failure(result, mem, False)
        details = verdict.handback_details["details"]
        assert "diagnostics" not in details, (
            "raw diagnostics key must not appear in handback details"
        )
        assert "diagnostics_preview" in details
        assert details["diagnostics_char_count"] == 50000
        assert details["diagnostics_truncated"] is True

    def test_small_diagnostics_remain_readable(self):
        """Short diagnostics pass through unchanged and not truncated."""
        small = "AssertionError: expected 3 got 5"
        result = validation_diagnostics_preview(small, limit=2000)
        assert result["diagnostics_preview"] == small
        assert result["diagnostics_truncated"] is False
        assert result["diagnostics_char_count"] == len(small)

    def test_empty_diagnostics(self):
        result = validation_diagnostics_preview("", limit=2000)
        assert result["diagnostics_preview"] == ""
        assert result["diagnostics_truncated"] is False
        assert result["diagnostics_char_count"] == 0

    def test_none_diagnostics(self):
        result = validation_diagnostics_preview(None, limit=2000)  # type: ignore[arg-type]
        assert result["diagnostics_preview"] == ""
        assert result["diagnostics_truncated"] is False
        assert result["diagnostics_char_count"] == 0


# ---------------------------------------------------------------------------
# Verdict helpers
# ---------------------------------------------------------------------------

def VERDICT_INSTRUCTION_NONEMPTY(v: ValidationFailureVerdict) -> bool:
    return bool(v.instruction.strip())


# ---------------------------------------------------------------------------
# Normalizer rejection — routes as fix_command, not repair
# ---------------------------------------------------------------------------

class TestNormalizerRejectionRouting:
    """Normalizer rejection (bare cd, export) produces infra_only results
    that route_validation_failure sends to fix_command."""

    def test_bare_cd_routes_to_fix_command(self):
        """Bare cd rejection has infra_only=True → action is fix_command."""
        run = ValidationRunResult(
            command="",
            raw_text="cd src",
            exit_code=None,
            output="Ambiguous shell construct: bare 'cd'",
            classification="validation_command_unrunnable",
            counts_as_product_failure=False,
        )
        result = WorkerFinalValidationResult(
            ok=False, diagnostics="Ambiguous shell construct: bare 'cd'",
            command="cd src", runs=[run],
        )
        assert result.infra_only is True
        verdict = route_validation_failure(result, {}, False)
        assert verdict.action == "fix_command"
        assert "Do not edit product code" in verdict.instruction

    def test_sandbox_exception_routes_to_fix_command(self):
        """Sandbox exception has infra_only=True → action is fix_command."""
        run = ValidationRunResult(
            command="pytest",
            raw_text="pytest",
            exit_code=None,
            output="RuntimeError: connection refused",
            classification="validation_command_unrunnable",
            counts_as_product_failure=False,
        )
        result = WorkerFinalValidationResult(
            ok=False, diagnostics="RuntimeError: connection refused",
            command="pytest", runs=[run],
        )
        assert result.infra_only is True
        verdict = route_validation_failure(result, {}, False)
        assert verdict.action == "fix_command"
        assert "Do not edit product code" in verdict.instruction


# ---------------------------------------------------------------------------
# Product failure still routes as repair — regression guard
# ---------------------------------------------------------------------------

class TestProductFailureRouting:
    """A real pytest/product failure still routes as repair."""

    def test_product_validation_routes_to_repair(self):
        run = ValidationRunResult(
            command="pytest",
            raw_text="pytest",
            exit_code=1,
            output="FAILED test_foo.py::test_bar - AssertionError",
            classification=PRODUCT_VALIDATION_FAILED,
            counts_as_product_failure=True,
        )
        result = WorkerFinalValidationResult(
            ok=False, diagnostics="FAILED test_foo.py::test_bar - AssertionError",
            command="pytest", runs=[run],
        )
        assert result.infra_only is False
        assert result.counts_as_product_failure is True
        verdict = route_validation_failure(result, {}, False)
        assert verdict.action == "repair"

    def test_product_failure_stops_iteration(self):
        """A product failure stops iteration while infra failures continue.
        This guards the route-level separation between product and infra."""
        product_run = ValidationRunResult(
            command="pytest", raw_text="pytest", exit_code=1,
            classification=PRODUCT_VALIDATION_FAILED,
            counts_as_product_failure=True,
            output="AssertionError: expected 3 got 5",
        )
        infra_run = ValidationRunResult(
            command="ruff", raw_text="ruff", exit_code=1,
            classification="validation_command_unrunnable",
            counts_as_product_failure=False,
            output="validation_command_unrunnable",
        )
        # When product failure is present, infra_only is False
        result = WorkerFinalValidationResult(
            ok=False, diagnostics="product fail", command="pytest",
            runs=[product_run, infra_run],
        )
        assert result.infra_only is False
        assert result.counts_as_product_failure is True
        verdict = route_validation_failure(result, {}, False)
        # Presence of any product failure → repair (even if some runs are infra)
        assert verdict.action == "repair"
