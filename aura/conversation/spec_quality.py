"""Fatal checks for Planner -> Worker dispatch requests."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SpecQualityResult:
    ok: bool
    errors: list[str]


def validate_worker_dispatch_spec(
    spec: str,
    acceptance: str,
    *,
    goal: str = "",
) -> SpecQualityResult:
    errors: list[str] = []

    if not goal.strip():
        errors.append("goal is required")
    if not spec.strip():
        errors.append("spec is required")
    if not acceptance.strip():
        errors.append("acceptance is required")
    else:
        # Reject clearly useless acceptance
        _vague = _is_vague_acceptance(acceptance)
        if _vague:
            errors.append(
                "acceptance needs a concrete observable check — got: "
                f'"{acceptance.strip()[:80]}"'
            )

    return SpecQualityResult(ok=not errors, errors=errors)


def _is_vague_acceptance(text: str) -> bool:
    """Return True if acceptance text is clearly useless."""
    t = text.strip().lower()
    # Exact matches
    if t in (
        "works", "done", "as requested", "make it good",
        "user is happy", "should work", "it works",
    ):
        return True
    # Empty-ish
    if not t or len(t) < 5:
        return True
    # Vague patterns with no observable action
    _vague_phrases = [
        "make it good", "as requested", "user is happy", "looks good",
        "should be fine", "works as expected", "done correctly",
    ]
    for phrase in _vague_phrases:
        if phrase in t and not any(
            c in t for c in (
                ":", "`", "pass", "fail", "compile", "check",
                "test", "import", "run", "exists", "verify",
                "inspect", "assert",
            )
        ):
            return True
    return False


__all__ = ["SpecQualityResult", "validate_worker_dispatch_spec", "_is_vague_acceptance"]
