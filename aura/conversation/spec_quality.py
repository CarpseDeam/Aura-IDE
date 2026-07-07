"""Fatal checks for Planner -> Worker dispatch requests."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class SpecQualityResult:
    ok: bool
    errors: list[str]


@dataclass(frozen=True)
class DispatchQualityResult:
    ok: bool
    errors: list[str]
    failure_constraint: str = ""


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


def validate_planner_dispatch(args: dict[str, Any], latest_user_text: str) -> DispatchQualityResult:
    """Check that the Planner's dispatch covers all explicit steps in the user request."""
    errors: list[str] = []

    # Validate structured validation_commands (top-level and per-item).
    _validate_structured_validation_commands(args, errors)

    # Count dispatch items
    work_artifact = args.get("work_artifact")
    if isinstance(work_artifact, dict):
        items = work_artifact.get("items")
        if isinstance(items, list):
            item_count = len(items)
        else:
            item_count = 1
    else:
        item_count = 1  # flat dispatch counts as 1 item

    # Detect expected step count from user text
    step_count = _detect_explicit_step_count(latest_user_text)

    if step_count is not None and step_count > item_count:
        errors.append(
            f"Planner dispatch has {item_count} item(s) but user request "
            f"indicates {step_count} distinct steps."
        )
        failure_constraint = (
            "CONSTRAINT FOR NEXT PLANNER ATTEMPT:\n"
            f"The user's request appears to describe {step_count} distinct steps, "
            f"but the dispatch only contains {item_count} work_artifact item(s). "
            "Re-plan with one work_artifact item per user-requested step. "
            "Split the work into separate artifact items so that each can be "
            "dispatched and reviewed independently."
        )
        return DispatchQualityResult(
            ok=False,
            errors=errors,
            failure_constraint=failure_constraint,
        )

    return DispatchQualityResult(ok=True, errors=[])


def _detect_explicit_step_count(text: str) -> int | None:
    """Detect explicit step/request counts in user text.

    Returns the detected number of steps, or None if no clear multi-step signal.
    """
    if not text.strip():
        return None

    # Pattern 1: Numbered list lines matching "1.", "2)", etc.
    numbered_lines = re.findall(r"^\s*\d+[.)]\s", text, re.MULTILINE)
    if len(numbered_lines) >= 2:
        return len(numbered_lines)

    # Pattern 2: Explicit count words like "two steps", "3 changes"
    count_patterns = [
        r"\b(one|1)\s+(step|change|thing|item|task|part|section)\b",
        r"\b(two|2)\s+(steps|changes|things|items|tasks|parts|sections)\b",
        r"\b(three|3)\s+(steps|changes|things|items|tasks|parts|sections)\b",
        r"\b(four|4)\s+(steps|changes|things|items|tasks|parts|sections)\b",
        r"\b(five|5)\s+(steps|changes|things|items|tasks|parts|sections)\b",
        r"\b(several|multiple|a few|various)\s+(steps|changes|things|items|tasks|parts|sections)\b",
    ]
    for pattern in count_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            word = match.group(1).lower()
            number_map = {
                "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "several": 3, "multiple": 3, "a few": 3, "various": 3,
            }
            if word in number_map:
                count = number_map[word]
                if count > 1:
                    return count
            try:
                count = int(word)
                if count > 1:
                    return count
            except ValueError:
                pass

    # Pattern 3: Sequential markers at line starts
    seq_markers = {"first", "second", "third", "fourth", "fifth",
                   "next", "then", "finally", "lastly", "last"}
    found_markers: set[str] = set()
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for marker in seq_markers:
            if line.lower().startswith(marker + ":"):
                found_markers.add(marker.lower())
    ordinal_order = {"first", "second", "third", "fourth", "fifth"}
    if found_markers:
        ordinals = found_markers & ordinal_order
        if len(ordinals) >= 2:
            return len(ordinals)
        if len(found_markers) >= 2:
            return len(found_markers)

    return None


def _validate_structured_validation_commands(args: dict[str, Any], errors: list[str]) -> None:
    """Validate structured validation_commands entries in dispatch *args*.

    Each entry must have a non-empty ``command``.  Supports both the new
    structured format (list of dicts) and legacy flat strings.  Mutates
    *errors* in-place.
    """
    # Top-level validation_commands
    _check_command_list(args.get("validation_commands"), "validation_commands", errors)

    # Per-item validation_commands inside work_artifact
    work_artifact = args.get("work_artifact")
    if isinstance(work_artifact, dict):
        items = work_artifact.get("items")
        if isinstance(items, list):
            for idx, item in enumerate(items):
                if isinstance(item, dict):
                    _check_command_list(
                        item.get("validation_commands"),
                        f"work_artifact.items[{idx}].validation_commands",
                        errors,
                    )


def _check_command_list(raw: Any, path: str, errors: list[str]) -> None:
    """Check that each entry in a validation_commands list is non-empty."""
    if not isinstance(raw, list):
        return
    for idx, entry in enumerate(raw):
        if isinstance(entry, dict):
            cmd = str(entry.get("command") or "").strip()
        elif isinstance(entry, str):
            cmd = entry.strip()
        else:
            cmd = ""
        if not cmd:
            errors.append(f"{path}[{idx}] has an empty command.")


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


__all__ = [
    "DispatchQualityResult",
    "SpecQualityResult",
    "validate_planner_dispatch",
    "validate_worker_dispatch_spec",
    "_is_vague_acceptance",
]
