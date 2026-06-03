from __future__ import annotations
import ast
import difflib
import re
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

class CraftIssueSeverity(str, Enum):
    HARD = "hard"
    SOFT = "soft"

class OwnershipContext(str, Enum):
    """Determines strictness of authorship checks."""
    AURA = "aura"        # Aura-managed file — run all authorship checks
    FOREIGN = "foreign"  # Foreign repo / small patch — suppress namespace ceremonies

class ChangeIntent(str, Enum):
    bug_fix = "bug_fix"
    feature = "feature"
    test = "test"
    refactor = "refactor"
    config = "config"
    unknown = "unknown"

def _normalize_dataclass_fields(value: Any) -> dict[str, list[str]]:
    """Safely coerce expected_dataclass_fields to dict[str, list[str]].
    
    If value is a dict, normalizes each value to list[str].
    If value is a list (old format) or None/missing, returns {}.
    """
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, val in value.items():
        if isinstance(val, list):
            result[str(key)] = [str(v) for v in val]
        else:
            result[str(key)] = []
    return result


@dataclass
class ExplicitSpecContract:
    """Formal contract between Planner and Worker.
    
    Captures what the Worker must produce and must not do.
    Populated from the Planner's dispatch fields and verified by ContractGate.
    """
    expected_public_symbols: list[str] = field(default_factory=list)
    expected_dataclass_fields: dict[str, list[str]] = field(default_factory=dict)
    forbidden_public_methods: list[str] = field(default_factory=list)
    forbidden_calls: list[str] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_public_symbols": list(self.expected_public_symbols),
            "expected_dataclass_fields": dict(self.expected_dataclass_fields),
            "forbidden_public_methods": list(self.forbidden_public_methods),
            "forbidden_calls": list(self.forbidden_calls),
            "required_outputs": list(self.required_outputs),
            "non_goals": list(self.non_goals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExplicitSpecContract":
        return cls(
            expected_public_symbols=list(data.get("expected_public_symbols", [])),
            expected_dataclass_fields=_normalize_dataclass_fields(data.get("expected_dataclass_fields")),
            forbidden_public_methods=list(data.get("forbidden_public_methods", [])),
            forbidden_calls=list(data.get("forbidden_calls", [])),
            required_outputs=list(data.get("required_outputs", [])),
            non_goals=list(data.get("non_goals", [])),
        )

@dataclass
class ProposalCapsule:
    path: Path
    language: str
    tool_name: str
    original_code: str
    proposed_code: str
    # 1-indexed, end-exclusive line ranges in proposed_code
    # e.g. (3, 7) means lines 3, 4, 5, 6
    changed_line_ranges: list[tuple[int, int]]
    intent: ChangeIntent = ChangeIntent.unknown
    is_new_file: bool = False
    new_symbols: list[str] = field(default_factory=list)
    expected_public_symbols: list[str] = field(default_factory=list)
    expected_dataclass_fields: dict[str, list[str]] = field(default_factory=dict)
    forbidden_public_methods: list[str] = field(default_factory=list)
    forbidden_calls: list[str] = field(default_factory=list)
    ownership_context: OwnershipContext = OwnershipContext.AURA
    ast_tree: ast.Module | None = None
    contract: ExplicitSpecContract | None = None

@dataclass
class CraftIssue:
    line: int
    column: int | None
    code: str
    message: str
    suggestion: str
    severity: CraftIssueSeverity = CraftIssueSeverity.HARD

@dataclass
class CraftDecision:
    approved: bool
    cleaned_code: str = ""
    issues: list[CraftIssue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def soft_issues(self) -> list[CraftIssue]:
        return [i for i in self.issues if i.severity == CraftIssueSeverity.SOFT]

    @property
    def hard_issues(self) -> list[CraftIssue]:
        return [i for i in self.issues if i.severity == CraftIssueSeverity.HARD]

@dataclass
class CompiledPatch:
    """A proposal that passed all compiler checks and is ready for approval."""
    capsule: ProposalCapsule
    cleaned_code: str
    checks_passed: list[str] = field(default_factory=list)
    checks_warned: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompilerBounce:
    """A proposal that was rejected with structured repair instructions."""
    capsule: ProposalCapsule
    issues: list[CraftIssue]
    repair_instructions: str
    attempt_number: int
    max_attempts: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompilerReject:
    """Final rejection after max retries exhausted. Halts gracefully."""
    capsule: ProposalCapsule
    issues: list[CraftIssue]
    total_attempts: int
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


def line_in_ranges(line: int, ranges: list[tuple[int, int]]) -> bool:
    for start, end in ranges:
        if start <= line < end:
            return True
    return False

def node_in_ranges(node: ast.AST, ranges: list[tuple[int, int]]) -> bool:
    """1-indexed, end-exclusive interval overlap: node_start < range_end and node_end > range_start."""
    node_start = getattr(node, "lineno", 1)
    node_end = (getattr(node, "end_lineno", node_start) or node_start) + 1
    for range_start, range_end in ranges:
        if node_start < range_end and node_end > range_start:
            return True
    return False


CONTEXT_PADDING = 1


def _extract_issue_target(issue: CraftIssue) -> str | None:
    """Extract the symbolic target from an issue message for stable key generation."""
    msg = issue.message.lower().strip()
    code = issue.code

    if code == "undefined-name":
        m = re.search(r"'([^']+)'", msg)
        if m:
            return m.group(1)
    elif code == "broken-import":
        m = re.search(r"'([^']+)'", msg)
        if m:
            target = m.group(1)
            rest = msg[m.end():]
            m2 = re.search(r"'([^']+)'", rest)
            if m2:
                target = f"{target}.{m2.group(1)}"
            return target
    elif code == "call-signature":
        m = re.search(r"'([^']+)'", msg)
        if m:
            return m.group(1)
    elif code == "missing-attribute":
        m = re.search(r"'([^']+)'", msg)
        if m:
            return m.group(1)
    return None


def normalize_message(msg: str) -> str:
    """Normalize issue message to make it format-insensitive and digit-independent."""
    msg = msg.lower().strip()
    msg = re.sub(r'\d+', 'N', msg)
    msg = msg.replace("'", "").replace('"', "")
    return msg


def compute_issue_key(issue: CraftIssue) -> str:
    """Build a stable key for matching a diagnostic across original vs proposed code.

    Priority:
    1. code + normalized_message + target (symbol/import name) — stable across line shifts
    2. code + normalized_message + line — fallback when no target extractable
    """
    target = _extract_issue_target(issue)
    norm_msg = normalize_message(issue.message)
    if target:
        return f"{issue.code}:{norm_msg}:{target}"
    return f"{issue.code}:{norm_msg}:L{issue.line}"


def line_near_changed_ranges(line: int, changed_ranges: list[range | tuple[int, int]], padding: int = CONTEXT_PADDING) -> bool:
    """Check if a line is on or within padding lines of any changed range."""
    for r in changed_ranges:
        if isinstance(r, range):
            start = r.start
            end = r.stop
        else:
            start, end = r
        padded_start = max(1, start - padding)
        padded_end = end + padding
        if padded_start <= line < padded_end:
            return True
    return False


def changed_line_ranges(original: str, proposed: str) -> list[range]:
    """Compute changed line ranges (1-indexed) in proposed code."""
    original_lines = original.splitlines()
    proposed_lines = proposed.splitlines()
    matcher = difflib.SequenceMatcher(None, original_lines, proposed_lines)
    ranges = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            ranges.append(range(j1 + 1, j2 + 1))
    return ranges


def filter_delta_issues(
    proposed_issues: list[CraftIssue],
    original_issues: list[CraftIssue],
    changed_ranges: list[range | tuple[int, int]],
    is_new_file: bool = False,
) -> list[CraftIssue]:
    """Filter reference/linter issues to only block newly introduced or changed-line issues.

    Never filters:
    - Syntax errors (handled before reference checking)
    - Contract gate issues (CONTRACT_*)
    - Unsafe operation checks (destructive_operation, codes starting with forbidden_)

    Pre-existing diagnostics are reported as caveats by the compiler and do not
    block unrelated writes.
    """
    if is_new_file or not original_issues:
        return proposed_issues

    original_keys: set[str] = set()
    for issue in original_issues:
        key = compute_issue_key(issue)
        original_keys.add(key)

    filtered: list[CraftIssue] = []

    for issue in proposed_issues:
        if (
            issue.code in ("syntax-error", "destructive_operation")
            or issue.code.startswith("CONTRACT_")
            or issue.code.startswith("forbidden_")
        ):
            filtered.append(issue)
            continue

        key = compute_issue_key(issue)
        is_pre_existing = key in original_keys

        if not is_pre_existing:
            filtered.append(issue)

    return filtered
