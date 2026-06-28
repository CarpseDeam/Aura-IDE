"""Deterministic Worker flow steering.

The harness is intentionally non-authoritative: it can only queue a compact
internal steering message. It never blocks tools, fails a task, or changes tool
results.
"""

from __future__ import annotations

import enum
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from aura.conversation.worker_flow_helpers import (
    BROAD_ORIENTATION_TOOLS,
    TARGETED_READ_TOOLS,
    WRITE_TOOLS,
    VALIDATION_TOOLS,
    _assistant_text,
    _content_text,
    _has_full_picture_plus_followup,
    _inventory_restatement_marker_count,
    _int_or_none,
    _parse_payload,
    _path_mentions,
    _payload_is_large,
    _planning_marker_count,
    _read_payload_items,
    _tool_call_name_args,
    _tool_paths,
    _write_was_applied,
)


WORKER_FLOW_STEERING_TEXT = (
    "Worker Flow: continue from the locked inventory. Stop restating the plan. "
    "Do not restart broad orientation. Use targeted reads only for exact "
    "missing facts. Make the next smallest safe edit now. Preserve protected "
    "control-flow regions and "
    "avoid whole-file reconstruction."
)

WORKER_FLOW_VALIDATION_REQUIRED_TEXT = (
    "Worker Flow: files were changed and validation has not run yet. Run the "
    "focused validation command now. Do not summarize or plan. Use "
    "run_terminal_command with the smallest relevant py_compile or pytest "
    "command, then finish only after it passes."
)


class WorkerFlowPhase(str, enum.Enum):
    orienting = "orienting"
    inventory_locked = "inventory_locked"
    editing = "editing"
    validating = "validating"
    repairing = "repairing"
    done = "done"


BROAD_ORIENTATION_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "read_files",
        "read_file_outline",
        "list_directory",
        "glob",
        "grep_search",
        "search_codebase",
    }
)

TARGETED_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file_range",
        "find_usages",
        "code_intel_outline",
        "code_intel_references",
        "code_intel_dependents",
    }
)

WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "patch_file", "delete_file"})
VALIDATION_TOOLS: frozenset[str] = frozenset({"run_terminal_command", "run_and_watch"})

_PATH_RE = re.compile(
    r"(?<![\w./\\-])(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\."
    r"(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml|css|html|go|rs|java|cs|cpp|hpp|h|c|sh|ps1|txt)"
    r"\b|\b[A-Za-z_][\w.-]*\.(?:py|js|ts|tsx|jsx|md|json|toml|yaml|yml)\b"
)
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
_PLANNING_MARKER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\blet\s+me\b", re.IGNORECASE),
    re.compile(r"\bnow\s+i\s+have\b", re.IGNORECASE),
    re.compile(r"\bfull\s+picture\b", re.IGNORECASE),
    re.compile(r"\bcomplete\s+picture\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+plan\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+verify\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+check\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+read\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+think\b", re.IGNORECASE),
    re.compile(r"\bi\s+need\s+to\s+be\s+careful\b", re.IGNORECASE),
    re.compile(r"\bi\s+should\s+be\s+careful\b", re.IGNORECASE),
    re.compile(r"\bactually\b", re.IGNORECASE),
    re.compile(r"\bwait\b", re.IGNORECASE),
)
_FULL_OR_COMPLETE_PICTURE_RE = re.compile(
    r"\b(?:full|complete)\s+picture\b",
    re.IGNORECASE,
)
_PICTURE_FOLLOWUP_RE = re.compile(
    r"\b(?:"
    r"let\s+me\s+(?:read|verify|check|plan|think|analy[sz]e)|"
    r"i\s+(?:need|should)\s+(?:read|verify|check|plan|think|analy[sz]e)|"
    r"check\s+(?:tests?|imports?|files?|usages?|references?)|"
    r"plan\s+(?:helpers?|module|hunks?|edits?|patches?)"
    r")\b",
    re.IGNORECASE,
)
_EXTRACTION_RE = re.compile(
    r"\b(?:extract|extraction|refactor|move-only|move\s+only|split\s+out|split\s+into|"
    r"move\s+helpers?|re-export|reexport)\b",
    re.IGNORECASE,
)
_PLAN_SAYS_RE = re.compile(r"\bplan\s+says\b", re.IGNORECASE)
_HUNK_MECHANICS_RE = re.compile(r"\bhunks?\b", re.IGNORECASE)
_PATCH_PLAN_MECHANICS_RE = re.compile(
    r"\b(?:plan\s+(?:helpers?|module|hunks?|edits?|patches?)|"
    r"patch\s+hunks?|hunk\s+plan)\b",
    re.IGNORECASE,
)
_IMPORT_HELPER_MECHANICS_RE = re.compile(
    r"\b(?:remove|add)\s+(?:imports?|regex(?:es)?|helpers?|functions?|constants?)\b",
    re.IGNORECASE,
)
_INVENTORY_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*]|\d+[.)])\s+.*\b"
    r"(?:file|function|helper|import|regex|constant|class|hunk|\.py)\b"
)
_WHOLE_FILE_REWRITE_RE = re.compile(
    r"\b(?:reconstruct\s+(?:the\s+)?(?:entire|complete|whole)\s+file|"
    r"write\s+(?:the\s+)?(?:whole|entire|complete)\s+file\s+from\s+scratch|"
    r"replace\s+(?:the\s+)?(?:complete|entire|whole)\s+file|"
    r"rewrite\s+dispatch\.py\s+wholesale|"
    r"rewrite\s+(?:the\s+)?(?:whole|entire|complete)\s+.+?file)\b",
    re.IGNORECASE | re.DOTALL,
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
    There is deliberately no fatal/blocking API.
    """

    def __init__(
        self,
        state: WorkerFlowState | None = None,
        *,
        large_file_bytes: int = 80_000,
        large_file_lines: int = 900,
    ) -> None:
        self.state = state or WorkerFlowState()
        self.large_file_bytes = large_file_bytes
        self.large_file_lines = large_file_lines

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
        if not self.should_restrict_broad_orientation() or name not in BROAD_ORIENTATION_TOOLS:
            return None
        return {
            "ok": False,
            "tool": name,
            "args": args if isinstance(args, dict) else {},
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

    def _maybe_steer_for_broad_read(self, path: str) -> None:
        if not self.state.inventory_locked:
            return
        count = self.state.broad_reads_by_path.get(path, 0)
        is_large = path in self.state.large_read_paths or path.endswith("dispatch.py")
        if is_large and count >= 2:
            self._queue_steering("broad_read")
            return
        if self.state.exact_targets_named and is_large:
            self._queue_steering("broad_read")
            return
        if count >= 3:
            self._queue_steering("broad_read")

    def _add_evidence(self, kind: str) -> None:
        self.state.inventory_evidence.add(kind)
        if not self.state.inventory_locked and len(self.state.inventory_evidence) >= 2:
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
        self._clear_broad_orientation_restriction()
        if self.state.pending_steering_reason in {"orientation", "broad_read"}:
            self.state.pending_steering_message = ""
            self.state.pending_steering_reason = ""

    def _queue_steering(self, reason: str) -> None:
        if reason in {"orientation", "broad_read"} and self.state.inventory_locked:
            self.state.broad_orientation_restricted = True
        if not self.state.pending_steering_message:
            self.state.pending_steering_message = WORKER_FLOW_STEERING_TEXT
            self.state.pending_steering_reason = reason

    def _clear_broad_orientation_restriction(self) -> None:
        self.state.broad_orientation_restricted = False


def _assistant_text(full_message: dict[str, Any] | str | None) -> str:
    if full_message is None:
        return ""
    if isinstance(full_message, str):
        return full_message
    if not isinstance(full_message, dict):
        return ""
    return _content_text(full_message.get("content")) + "\n" + _content_text(full_message.get("reasoning_content"))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    return ""


def _planning_marker_count(text: str) -> int:
    return sum(len(pattern.findall(text)) for pattern in _PLANNING_MARKER_RES)


def _inventory_restatement_marker_count(text: str) -> int:
    count = 0
    count += len(_PLAN_SAYS_RE.findall(text))
    count += len(_HUNK_MECHANICS_RE.findall(text))
    count += len(_PATCH_PLAN_MECHANICS_RE.findall(text))
    count += len(_IMPORT_HELPER_MECHANICS_RE.findall(text))
    inventory_lines = len(_INVENTORY_LINE_RE.findall(text))
    if inventory_lines >= 3:
        count += inventory_lines
    return count


def _has_full_picture_plus_followup(text: str) -> bool:
    return bool(
        _FULL_OR_COMPLETE_PICTURE_RE.search(text)
        and _PICTURE_FOLLOWUP_RE.search(text)
    )


def _tool_call_name_args(tool_call: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return "", {}
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return "", {}
    name = str(function.get("name") or "")
    raw_args = function.get("arguments") or "{}"
    if isinstance(raw_args, dict):
        return name, raw_args
    if not isinstance(raw_args, str):
        return name, {}
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError:
        return name, {}
    return name, parsed if isinstance(parsed, dict) else {}


def _path_mentions(text: str) -> list[str]:
    return [_normalize_path(match.group(0)) for match in _PATH_RE.finditer(text)]


def _tool_paths(
    name: str,
    args: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> list[str]:
    paths: list[str] = []
    for key in ("path", "rel_path", "file", "target"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value)
    raw_paths = args.get("paths")
    if isinstance(raw_paths, list):
        paths.extend(str(path) for path in raw_paths if str(path).strip())
    if name == "glob" and isinstance(args.get("pattern"), str):
        paths.append(f"glob:{args['pattern']}")
    if name in {"grep_search", "search_codebase"}:
        for key in ("path", "path_filter", "include_glob", "glob"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value)

    if payload:
        for key in ("path", "rel_path"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value)
        files = payload.get("files")
        if isinstance(files, dict):
            paths.extend(str(key) for key in files if str(key).strip())

    normalized = [_normalize_path(path) for path in paths if _normalize_path(path)]
    return list(dict.fromkeys(normalized))


def _normalize_path(path: str) -> str:
    normalized = str(path).strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _parse_payload(result: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str) or not result.strip():
        return {}
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_payload_items(
    name: str,
    args: dict[str, Any],
    payload: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    if payload:
        path = _first_path(payload) or next(iter(_tool_paths(name, args)), "")
        if path:
            items.append((path, payload))
        files = payload.get("files")
        if isinstance(files, dict):
            for raw_path, item in files.items():
                if isinstance(item, dict):
                    items.append((_normalize_path(str(raw_path)), item))
    return items


def _first_path(payload: dict[str, Any]) -> str:
    for key in ("path", "rel_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_path(value)
    return ""


def _payload_is_large(payload: dict[str, Any], large_file_bytes: int, large_file_lines: int) -> bool:
    file_size = _int_or_none(payload.get("file_size"))
    if file_size is not None and file_size >= large_file_bytes:
        return True
    total_lines = _int_or_none(payload.get("total_lines"))
    if total_lines is not None and total_lines >= large_file_lines:
        return True
    content = payload.get("content")
    return isinstance(content, str) and len(content) >= large_file_bytes


def _tool_def_name(tool_def: dict[str, Any]) -> str:
    function = tool_def.get("function")
    return str(function.get("name") or "") if isinstance(function, dict) else ""


def _tool_result_succeeded(ok: bool | None, payload: dict[str, Any]) -> bool:
    return ok is True or payload.get("ok") is True


def _write_was_applied(name: str, ok: bool | None, payload: dict[str, Any]) -> bool:
    if ok is False:
        return False
    if payload.get("applied") is True:
        return True
    if payload.get("applied") is False:
        return False
    if payload.get("ok") is True and name in WRITE_TOOLS:
        return True
    return bool(ok and not payload)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BROAD_ORIENTATION_TOOLS",
    "TARGETED_READ_TOOLS",
    "VALIDATION_TOOLS",
    "WORKER_FLOW_VALIDATION_REQUIRED_TEXT",
    "WORKER_FLOW_STEERING_TEXT",
    "WRITE_TOOLS",
    "WorkerFlowHarness",
    "WorkerFlowPhase",
    "WorkerFlowState",
]
