"""Shared write-result payload markers with no registry or mixin dependencies."""


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
