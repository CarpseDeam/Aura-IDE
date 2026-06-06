from __future__ import annotations
import ast
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
    task_shape: Any | None = None

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
