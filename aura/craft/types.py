from __future__ import annotations
import ast
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field

class CraftIssueSeverity(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class ChangeIntent(str, Enum):
    bug_fix = "bug_fix"
    feature = "feature"
    test = "test"
    refactor = "refactor"
    config = "config"
    unknown = "unknown"

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


@dataclass
class CompilerBounce:
    """A proposal that was rejected with structured repair instructions."""
    capsule: ProposalCapsule
    issues: list[CraftIssue]
    repair_instructions: str
    attempt_number: int
    max_attempts: int


@dataclass
class CompilerReject:
    """Final rejection after max retries exhausted. Halts gracefully."""
    capsule: ProposalCapsule
    issues: list[CraftIssue]
    total_attempts: int
    reason: str


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
