from __future__ import annotations
import ast
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field

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

@dataclass
class CraftIssue:
    line: int
    column: int | None
    code: str
    message: str
    suggestion: str

@dataclass
class CraftDecision:
    approved: bool
    cleaned_code: str = ""
    issues: list[CraftIssue] = field(default_factory=list)

def line_in_ranges(line: int, ranges: list[tuple[int, int]]) -> bool:
    for start, end in ranges:
        if start <= line < end:
            return True
    return False

def node_in_ranges(node: ast.AST, ranges: list[tuple[int, int]]) -> bool:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    for cs, ce in ranges:
        if (cs <= start <= ce) or (cs <= end <= ce) or (start <= cs <= end) or (start <= ce <= end):
            return True
    return False
