"""Deterministic Worker flow steering and lightweight action ratchets.

The harness tracks Worker flow state, queues compact internal steering, filters
temporary broad-orientation tools, returns recoverable blocks for stale broad
calls, and requires validation before a final report after successful writes.
"""

from __future__ import annotations

import enum
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from aura.conversation.worker_flow_helpers import (
    BROAD_ORIENTATION_TOOLS,
    TARGETED_READ_TOOLS,
    VALIDATION_TOOLS,
    WRITE_TOOLS,
    _assistant_text,
    _has_full_picture_plus_followup,
    _inventory_restatement_marker_count,
    _parse_payload,
    _path_mentions,
    _payload_is_large,
    _planning_marker_count,
    _read_payload_items,
    _tool_call_name_args,
    _tool_def_name,
    _tool_paths,
    _tool_result_succeeded,
    _write_was_applied,
)

WORKER_FLOW_STEERING_TEXT = (
    "Worker Flow: continue from the locked inventory. Stop restating the plan. "
    "Do not restart broad orientation. For large refactors, use the existing "
    "outline plus at most one or two targeted ranges for exact missing facts. "
    "Make the next smallest safe edit now: create the first module, patch the "
    "first seam, or wire back one helper family. Preserve protected control-flow "
    "regions and avoid whole-file reconstruction."
)

WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT = (
    "Worker Flow zero-work recovery: use the context already gathered and make "
    "one smallest safe edit now. Do not broaden orientation, do not restate the "
    "plan, and do not produce a final report with zero changes. You may perform "
    "one targeted read only if a specific missing fact is required for the first "
    "edit. If no safe edit is possible, return a real blocker with the exact "
    "missing fact, file/path, permission/tool failure, or planner-resolution "
    "question."
)

WORKER_FLOW_LARGE_FILE_SEAM_TEXT = (
    "Worker Flow: large-file seam work is drifting into line-number archaeology. "
    "Stop calculating method boundaries by repeated reads. Use read_file_outline "
    "or code_intel_outline to anchor symbols, then one or two read_file_range calls "
    "around the current symbols. Patch one seam only, or return "
    "needs_planner_resolution if the seam cannot be located quickly."
)

WORKER_FLOW_LARGE_SOURCE_READ_TEXT = (
    "Worker Flow: do not full-read known large source files. Use read_file_outline "
    "to anchor symbols, then read_file_range around the smallest relevant seam."
)

WORKER_FLOW_VALIDATION_REQUIRED_TEXT = (
    "Worker Flow: files were changed and validation has not run yet. Run the "
    "focused validation command now. Do not summarize or plan. Use "
    "run_terminal_command with the smallest relevant py_compile or pytest "
    "command, then finish only after it passes."
)

KNOWN_LARGE_SOURCE_PATHS: frozenset[str] = frozenset(
    {
        "aura/conversation/dispatch.py",
        "aura/conversation/manager.py",
    }
)


class WorkerFlowPhase(str, enum.Enum):
    orienting = "orienting"
    inventory_locked = "inventory_locked"
    editing = "editing"
    validating = "validating"
    repairing = "repairing"
    done = "done"


_CREATE_MODULE_RE = re.compile(
    r"\b(?:create|add|new|split\s+into|extract\s+into)\b.{0,100}\.[a-z0-9]{1,6}\b",
    re.IGNORECASE | re.DOTALL,
)
_MOVE_HELPERS_RE = re.compile(
    r"\b(?:move|extract|split|re-export|reexport)\b.{0,120}"
    r"\b(?:function|functions|class|classes|helper|helpers|method|methods|_[A-Za-z]\w*|[A-Za-z_]\w+\()",
    re.IGNORECASE | re.DOTALL,
)
_PROTECTED_RE = re.compile(
    r"\b(?:do\s+not\s+touch|don't\s+touch|preserve|protected|leave\s+.+?\s+unchanged|"
    r"avoid\s+touching|control[-\s]?flow)\b",
    re.IGNORECASE | re.DOTALL,
)
_VALIDATION_RE = re.compile(
    r"\b(?:python\s+-m|pytest|py_compile|ruff|mypy|tox|npm\s+test|pnpm\s+test|"
    r"run_terminal_command|run_and_watch)\b",
    re.IGNORECASE,
)
_NEXT_ACTION_RE = re.compile(
    r"\b(?:next\s+(?:i|step)|i(?:'ll|\s+will)\s+(?:patch|edit|create|move|extract|run)|"
    r"use\s+patch_file|call\s+patch_file|start\s+by\s+patching|now\s+patch|then\s+patch)\b",
    re.IGNORECASE,
)
_EXACT_TARGET_RE = re.compile(
    r"\b(?:read_file_range|lines?\s+\d+|def\s+[A-Za-z_]\w+|class\s+[A-Za-z_]\w+|"
    r"_[A-Za-z]\w+|[A-Za-z_]\w+\(\))\b",
    re.IGNORECASE,
)
_PLANNING_RE = re.compile(
    r"\b(?:complete\s+picture|full\s+picture|now\s+i\s+have\s+.+?picture|"
    r"let\s+me\s+re-?read|i\s+need\s+to\s+re-?read|i(?:'ll|\s+will)\s+re-?read|"
    r"let\s+me\s+plan|plan\s+is)\b",
    re.IGNORECASE | re.DOTALL,
)
_EXTRACTION_RE = re.compile(
    r"\b(?:extract|extraction|refactor|move-only|move\s+only|split\s+out|split\s+into|"
    r"move\s+helpers?|re-export|reexport)\b",
    re.IGNORECASE,
)
_WHOLE_FILE_REWRITE_RE = re.compile(
    r"\b(?:reconstruct\s+(?:the\s+)?(?:entire|complete|whole)\s+file|"
    r"write\s+(?:the\s+)?(?:whole|entire|complete)\s+file\s+from\s+scratch|"
    r"replace\s+(?:the\s+)?(?:complete|entire|whole)\s+file|"
    r"rewrite\s+dispatch\.py\s+wholesale|"
    r"rewrite\s+(?:the\s+)?(?:whole|entire|complete)\s+.+?file)\b",
    re.IGNORECASE | re.DOTALL,
)
_LINE_NUMBER_ARCHAEOLOGY_RE = re.compile(
    r"\b(?:exact\s+line\s+numbers?|line\s+numbers?\s+(?:for|i\s+need)|"
    r"exact\s+(?:method\s+)?boundar(?:y|ies)|find\s+the\s+exact\s+boundar(?:y|ies)|"
    r"calculate\s+exact\s+line\s+ranges?|trace\s+line\s+by\s+line|"
    r"starts?\s+(?:at|around)\s+line\s+\d+|ends?\s+(?:at|around)\s+line\s+\d+|"
    r"read\s+(?:known\s+)?ranges?|read\s+around\s+lines?)\b",
    re.IGNORECASE,
)


@dataclass
class WorkerFlowState:
    phase: WorkerFlowPhase = WorkerFlowPhase.orienting
    inventory_locked: bool = False
    inventory_evidence: set[str] = field(default_factory=set)
    locked_inventory: tuple[str, ...] = ()
    broad_reads_by_path: Counter[str] = field(default_factory=Counter)
    targeted_reads_by_path: Counter[str] = field(default_factory=Counter)
    write_intents: int = 0
    write_actions: int = 0
    validation_intents: int = 0
    validation_actions: int = 0
    planning_restatements_since_write: int = 0
    extraction_inventory_restatements_since_write: int = 0
    line_number_archaeology_mentions: int = 0
    large_read_paths: set[str] = field(default_factory=set)
    exact_targets_named: bool = False
    extraction_or_refactor: bool = False
    protected_large_file_danger_signs: int = 0
    broad_orientation_restricted: bool = False
    validation_required_before_final: bool = False
    pending_steering_message: str = ""
    pending_steering_reason: str = ""


class WorkerFlowHarness:
    """Ratcheting flow state for worker mode.

    The public methods tolerate malformed inputs and only mutate local state.
    Recoverable tool blocks are policy hints; the harness has no fatal outcome.
    """

    def __init__(
        self,
        state: WorkerFlowState | None = None,
        *,
        large_file_bytes: int = 80_000,
        large_file_lines: int = 900,
        large_source_paths: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.state = state or WorkerFlowState()
        self.large_file_bytes = large_file_bytes
        self.large_file_lines = large_file_lines
        self.large_source_paths = frozenset(
            self._normalize_path(path)
            for path in (large_source_paths or KNOWN_LARGE_SOURCE_PATHS)
        )

    @property
    def pending_steering_message(self) -> str:
        return self.state.pending_steering_message

    @property
    def fatal_outcome(self) -> None:
        return None

    @property
    def blocking_outcome(self) -> None:
        return None

    def has_fatal_outcome(self) -> bool:
        return False

    def has_blocking_outcome(self) -> bool:
        return False

    def should_steer(self) -> bool:
        return bool(self.state.pending_steering_message)

    def filter_tool_defs(self, tool_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.should_restrict_broad_orientation():
            return tool_defs
        return [
            tool_def
            for tool_def in tool_defs
            if _tool_def_name(tool_def) not in BROAD_ORIENTATION_TOOLS
        ]

    def should_restrict_broad_orientation(self) -> bool:
        return bool(self.state.broad_orientation_restricted)

    def should_block_tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any] | None:
        args = args if isinstance(args, dict) else {}
        large_source_paths = self._blocked_large_source_full_read_paths(name, args)
        if large_source_paths:
            self._queue_large_source_read_steering()
            return {
                "ok": False,
                "tool": name,
                "args": args,
                "failure_class": "worker_flow_large_source_full_read_restricted",
                "recoverable": True,
                "internal_recovery_steer": True,
                "worker_flow_block": True,
                "blocked_paths": large_source_paths,
                "error": (
                    "Worker Flow blocked a full-file read of known large source "
                    f"file(s): {', '.join(large_source_paths)}."
                ),
                "suggested_tool": "read_file_outline",
                "suggested_next_tool": "read_file_outline",
                "suggested_next_action": (
                    "Use read_file_outline on the large source file first, then "
                    "read_file_range around the smallest relevant symbol or seam. "
                    "Split unrelated small-file reads into a separate read_file/read_files call."
                ),
                "allowed_tool_groups": {
                    "targeted_reads": sorted(TARGETED_READ_TOOLS),
                    "writes": sorted(WRITE_TOOLS),
                    "validation": sorted(VALIDATION_TOOLS),
                },
            }
        if not self.should_restrict_broad_orientation() or name not in BROAD_ORIENTATION_TOOLS:
            return None
        return {
            "ok": False,
            "tool": name,
            "args": args,
            "failure_class": "worker_flow_broad_orientation_restricted",
            "recoverable": True,
            "internal_recovery_steer": True,
            "worker_flow_block": True,
            "error": (
                "Worker Flow temporarily blocked broad orientation after the "
                "inventory was locked and re-orientation was detected."
            ),
            "suggested_tool": "read_file_range",
            "suggested_next_tool": "read_file_range",
            "suggested_next_action": (
                "Use targeted reads for exact missing facts, patch_file/write_file/delete_file "
                "for edits, or run_terminal_command/run_and_watch for focused validation."
            ),
            "allowed_tool_groups": {
                "targeted_reads": sorted(TARGETED_READ_TOOLS),
                "writes": sorted(WRITE_TOOLS),
                "validation": sorted(VALIDATION_TOOLS),
            },
        }

    def requires_validation_before_final(self) -> bool:
        return bool(self.state.validation_required_before_final)

    def mark_validation_satisfied(self) -> None:
        self.state.validation_required_before_final = False
        self._clear_broad_orientation_restriction()

    def mark_non_thrashing(self) -> None:
        self._clear_broad_orientation_restriction()

    def observe_assistant_message(self, full_message: dict[str, Any] | str | None) -> None:
        text = _assistant_text(full_message)
        if text:
            self._observe_assistant_text(text)

        if isinstance(full_message, dict):
            for tool_call in full_message.get("tool_calls") or []:
                name, args = _tool_call_name_args(tool_call)
                if name:
                    self._observe_tool_call_evidence(name, args)

    def observe_tool_call(self, name: str, args: dict[str, Any] | None = None) -> None:
        args = args if isinstance(args, dict) else {}
        self._observe_tool_call_evidence(name, args)

        if name in BROAD_ORIENTATION_TOOLS:
            for path in _tool_paths(name, args):
                self.state.broad_reads_by_path[path] += 1
                self._maybe_steer_for_broad_read(path)
            return

        if name in TARGETED_READ_TOOLS:
            for path in _tool_paths(name, args):
                self.state.targeted_reads_by_path[path] += 1
            return

        if name in WRITE_TOOLS:
            self.state.write_intents += 1
            self._advance_to(WorkerFlowPhase.editing)
            self._reduce_orientation_pressure()
            return

        if name in VALIDATION_TOOLS:
            self.state.validation_intents += 1
            self._add_evidence("validation_commands")
            self._clear_broad_orientation_restriction()

    def observe_tool_result(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        ok: bool | None = None,
        result: str | dict[str, Any] | None = None,
    ) -> None:
        if isinstance(args, bool) and result is None:
            result = ok
            ok = args
            args = {}
        elif args is not None and not isinstance(args, dict) and ok is None and result is None:
            result = args
            args = {}
        args = args if isinstance(args, dict) else {}
        payload = _parse_payload(result)

        if name in BROAD_ORIENTATION_TOOLS:
            self._observe_large_read_payload(name, args, payload)
            for path in _tool_paths(name, args, payload):
                self._maybe_steer_for_broad_read(path)
            return

        if name in WRITE_TOOLS:
            if _write_was_applied(name, ok, payload):
                self.state.write_actions += 1
                self.state.validation_required_before_final = True
                self._advance_to(WorkerFlowPhase.editing)
                self._reduce_orientation_pressure()
            return

        if name in VALIDATION_TOOLS:
            self.state.validation_actions += 1
            self._advance_to(WorkerFlowPhase.validating)
            self._clear_broad_orientation_restriction()
            if _tool_result_succeeded(ok, payload):
                self.mark_validation_satisfied()

    def pop_pending_steering(self) -> str:
        message = self.state.pending_steering_message
        self.state.pending_steering_message = ""
        self.state.pending_steering_reason = ""
        return message

    def _observe_assistant_text(self, text: str) -> None:
        was_inventory_locked = self.state.inventory_locked
        paths = _path_mentions(text)
        if paths:
            self._add_evidence("target_files")
            if len(paths) >= 2:
                self._add_evidence("files_or_modules")

        if _CREATE_MODULE_RE.search(text):
            self._add_evidence("files_or_modules")
        if _MOVE_HELPERS_RE.search(text):
            self._add_evidence("functions_or_helpers")
        if _PROTECTED_RE.search(text):
            self._add_evidence("protected_regions")
        if _VALIDATION_RE.search(text):
            self._add_evidence("validation_commands")
        if _NEXT_ACTION_RE.search(text):
            self._add_evidence("explicit_next_action")
        if _EXACT_TARGET_RE.search(text):
            self.state.exact_targets_named = True

        extraction_inventory = self._looks_like_extraction_inventory(text, paths)
        if extraction_inventory:
            self.state.extraction_or_refactor = True
            self._add_evidence("functions_or_helpers")
            if len(paths) >= 2:
                self._add_evidence("files_or_modules")

        if was_inventory_locked:
            planning_marker_count = _planning_marker_count(text)
            inventory_marker_count = _inventory_restatement_marker_count(text)
            has_planning_restatement = bool(
                planning_marker_count or _PLANNING_RE.search(text)
            )
            if has_planning_restatement:
                self.state.planning_restatements_since_write += 1
                if self.state.planning_restatements_since_write >= 2:
                    self._queue_steering("orientation")
            if planning_marker_count >= 3:
                self._queue_steering("orientation")
            if _has_full_picture_plus_followup(text):
                self._queue_steering("orientation")
            if _EXTRACTION_RE.search(text) and inventory_marker_count >= 2:
                self._queue_steering("orientation")

            if extraction_inventory:
                self.state.extraction_inventory_restatements_since_write += 1
                if self.state.extraction_inventory_restatements_since_write >= 2:
                    self._queue_steering("orientation")

        if self._looks_like_whole_file_rewrite_danger(text):
            self.state.protected_large_file_danger_signs += 1
            self._queue_steering("whole_file_rewrite")

        line_archaeology_count = len(_LINE_NUMBER_ARCHAEOLOGY_RE.findall(text))
        if line_archaeology_count and self._looks_like_large_file_seam_work(text, paths):
            self.state.line_number_archaeology_mentions += line_archaeology_count
            if line_archaeology_count >= 2 or self.state.line_number_archaeology_mentions >= 2:
                self._queue_steering("line_number_archaeology")

    def _observe_tool_call_evidence(self, name: str, args: dict[str, Any]) -> None:
        if name in BROAD_ORIENTATION_TOOLS or name in TARGETED_READ_TOOLS or name in WRITE_TOOLS:
            if _tool_paths(name, args):
                self._add_evidence("target_files")
        if name in WRITE_TOOLS:
            self._add_evidence("explicit_next_action")
        if name in VALIDATION_TOOLS:
            self._add_evidence("validation_commands")

    def _observe_large_read_payload(
        self,
        name: str,
        args: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        for path, item in _read_payload_items(name, args, payload):
            if _payload_is_large(item, self.large_file_bytes, self.large_file_lines):
                self.state.large_read_paths.add(path)

    def _looks_like_extraction_inventory(self, text: str, paths: list[str]) -> bool:
        lower = text.lower()
        if not _EXTRACTION_RE.search(text):
            return False
        if len(paths) >= 3:
            return True
        if "worker_report.py" in lower or "worker_outcome.py" in lower or "worker_hygiene.py" in lower:
            return True
        if "preserve _run_worker control flow" in lower:
            return True
        if "re-export helpers from dispatch.py" in lower or "reexport helpers from dispatch.py" in lower:
            return True
        if "focused validation" in lower and _VALIDATION_RE.search(text):
            return True
        return bool(paths and _MOVE_HELPERS_RE.search(text))

    def _looks_like_whole_file_rewrite_danger(self, text: str) -> bool:
        if not _WHOLE_FILE_REWRITE_RE.search(text):
            return False
        lower = text.lower()
        return (
            self.state.extraction_or_refactor
            or bool(_EXTRACTION_RE.search(text))
            or "dispatch.py" in lower
            or "move-only" in lower
            or "move only" in lower
        )

    def _looks_like_large_file_seam_work(self, text: str, paths: list[str]) -> bool:
        lower = text.lower()
        if any(path.endswith(("manager.py", "dispatch.py")) for path in paths):
            return True
        if "manager.py" in lower or "dispatch.py" in lower:
            return True
        return self.state.extraction_or_refactor or bool(_EXTRACTION_RE.search(text))

    def _maybe_steer_for_broad_read(self, path: str) -> None:
        if not self.state.inventory_locked:
            return
        count = self.state.broad_reads_by_path.get(path, 0)
        is_large = path in self.state.large_read_paths or path.endswith("dispatch.py")
        if is_large and count >= 2:
            self._queue_steering("broad_read")
            return
        if self.state.exact_targets_named and is_large and count >= 2:
            self._queue_steering("broad_read")
            return
        if count >= 3:
            self._queue_steering("broad_read")

    def _add_evidence(self, kind: str) -> None:
        self.state.inventory_evidence.add(kind)
        required_evidence = 3 if self.state.extraction_or_refactor else 2
        if (
            not self.state.inventory_locked
            and len(self.state.inventory_evidence) >= required_evidence
        ):
            self.state.inventory_locked = True
            self.state.locked_inventory = tuple(sorted(self.state.inventory_evidence))
            if self.state.phase == WorkerFlowPhase.orienting:
                self.state.phase = WorkerFlowPhase.inventory_locked

    def _advance_to(self, phase: WorkerFlowPhase) -> None:
        if not self.state.inventory_locked:
            self.state.inventory_locked = True
            self.state.locked_inventory = tuple(sorted(self.state.inventory_evidence))
        self.state.phase = phase

    def _reduce_orientation_pressure(self) -> None:
        self.state.planning_restatements_since_write = 0
        self.state.extraction_inventory_restatements_since_write = 0
        self.state.line_number_archaeology_mentions = 0
        self._clear_broad_orientation_restriction()
        if self.state.pending_steering_reason in {"orientation", "broad_read", "line_number_archaeology"}:
            self.state.pending_steering_message = ""
            self.state.pending_steering_reason = ""

    def _queue_steering(self, reason: str) -> None:
        if reason in {"orientation", "broad_read", "line_number_archaeology"} and self.state.inventory_locked:
            self.state.broad_orientation_restricted = True
        if not self.state.pending_steering_message:
            self.state.pending_steering_message = (
                WORKER_FLOW_LARGE_FILE_SEAM_TEXT
                if reason == "line_number_archaeology"
                else WORKER_FLOW_STEERING_TEXT
            )
            self.state.pending_steering_reason = reason

    def _queue_large_source_read_steering(self) -> None:
        if not self.state.pending_steering_message:
            self.state.pending_steering_message = WORKER_FLOW_LARGE_SOURCE_READ_TEXT
            self.state.pending_steering_reason = "large_source_full_read"

    def _clear_broad_orientation_restriction(self) -> None:
        self.state.broad_orientation_restricted = False

    def _blocked_large_source_full_read_paths(self, name: str, args: dict[str, Any]) -> list[str]:
        if name not in {"read_file", "read_files"}:
            return []
        return [
            path
            for path in _tool_paths(name, args)
            if self._normalize_path(path) in self.large_source_paths
        ]

    def _normalize_path(self, path: str) -> str:
        normalized = str(path).strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized


__all__ = [
    "BROAD_ORIENTATION_TOOLS",
    "KNOWN_LARGE_SOURCE_PATHS",
    "TARGETED_READ_TOOLS",
    "VALIDATION_TOOLS",
    "WORKER_FLOW_LARGE_SOURCE_READ_TEXT",
    "WORKER_FLOW_VALIDATION_REQUIRED_TEXT",
    "WORKER_FLOW_STEERING_TEXT",
    "WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT",
    "WORKER_FLOW_LARGE_FILE_SEAM_TEXT",
    "WRITE_TOOLS",
    "WorkerFlowHarness",
    "WorkerFlowPhase",
    "WorkerFlowState",
]
