"""Canonical Execution Checklist core.

This package owns pure checklist data models and construction rules for the
visible execution checklist. It does not know about Qt, DispatchSession wiring,
or Worker prompting.
"""

from aura.execution_checklist.builder import (
    build_execution_checklist,
    build_execution_checklist_items,
    normalize_execution_checklist,
    raw_execution_checklist,
)
from aura.execution_checklist.controller import ExecutionChecklistController
from aura.execution_checklist.models import (
    ExecutionChecklistItem,
    ExecutionChecklistSnapshot,
    ExecutionChecklistStatus,
    VALID_STATUSES,
)
from aura.execution_checklist.validation import (
    compact_checklist_label,
    is_implementation_detail,
    request_is_non_trivial,
    validate_checklist_items,
)

__all__ = [
    "ExecutionChecklistItem",
    "ExecutionChecklistSnapshot",
    "ExecutionChecklistStatus",
    "ExecutionChecklistController",
    "VALID_STATUSES",
    "build_execution_checklist",
    "build_execution_checklist_items",
    "compact_checklist_label",
    "is_implementation_detail",
    "normalize_execution_checklist",
    "raw_execution_checklist",
    "request_is_non_trivial",
    "validate_checklist_items",
]
