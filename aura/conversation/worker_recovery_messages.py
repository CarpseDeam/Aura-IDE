"""Pure Worker recovery instruction message templates, extracted from ConversationManager."""
from __future__ import annotations

__all__ = [
    "PATCH_CANDIDATE_INVALID_SYNTAX_ACTION",
    "WORKER_EDIT_RECOVERY_INSTRUCTION",
    "WORKER_AUTO_PY_COMPILE_INSTRUCTION",
    "WORKER_IMPORT_FAILURE_INSTRUCTION",
    "WORKER_DEPENDENT_CONTRACT_INSTRUCTION",
    "WORKER_LAUNCH_FAILURE_INSTRUCTION",
    "WORKER_BATCHED_VALIDATION_INSTRUCTION",
]

PATCH_CANDIDATE_INVALID_SYNTAX_ACTION = (
    "The proposed patch candidate would make this Python file invalid. The live file was not changed. "
    "Re-read the suggested range, then retry patch_file once with a larger exact old block that includes "
    "the adjacent line before and after the edit. Do not analyze patch mechanics. If the retry fails, "
    "return a concise blocker."
)

WORKER_EDIT_RECOVERY_INSTRUCTION = (
    "Previous edit failed recoverably. Re-read the affected file with read_file or read_file_range, "
    "then retry patch_file once with corrected hunks and the current expected_file_hash. "
    "Do not use write_file as a fallback for an existing-file edit. "
    "Finish only after the edit is applied and touched Python files pass py_compile."
)

WORKER_AUTO_PY_COMPILE_INSTRUCTION = (
    "Focused py_compile failed on the following Python file(s). "
    "Re-read and repair the file(s), then run python -m py_compile again. "
    "Finish only after py_compile passes.\n\n"
    "Diagnostic output:\n{diagnostics}"
)

WORKER_IMPORT_FAILURE_INSTRUCTION = (
    "Import check failed on the following Python file(s). "
    "The module(s) raised an exception on import, which means a symbol, import, "
    "or dependency is missing or broken. Re-read and repair the file(s), "
    "then finish again.\n\n"
    "Diagnostic output:\n{diagnostics}"
)

WORKER_DEPENDENT_CONTRACT_INSTRUCTION = (
    "Import check failed on downstream dependent(s) because of a contract change "
    "in the edited file(s). The edit changed a symbol, name, or signature that "
    "the following dependent(s) import. Either restore that contract in the "
    "edited file(s) or update the dependent(s) to match, then finish again.\n\n"
    "Edited: {edited_files}\n"
    "Broken dependents: {dependent_files}\n\n"
    "Diagnostic output:\n{diagnostics}"
)

WORKER_LAUNCH_FAILURE_INSTRUCTION = (
    "Launch verification failed. The declared run_command did not complete "
    "successfully: it either exited nonzero, printed a traceback/error, was "
    "cancelled, or was still running when the watch window expired. The import "
    "checks passed, so the code is structurally valid, but it fails at runtime. "
    "Re-read the relevant file(s), repair the failure shown in the output below, "
    "then finish again.\n\n"
    "Command: {command}\n\n"
    "Diagnostic output:\n{output}"
)

WORKER_BATCHED_VALIDATION_INSTRUCTION = (
    "Validation found {num_problems} problems. All must be fixed before finishing.\n\n"
    "{findings_sections}\n"
    "Fix all of the above problems, re-run the smallest relevant check per fix, "
    "and only then provide the final report."
)
