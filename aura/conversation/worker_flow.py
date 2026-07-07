"""Honest Worker accounting — no prose surveillance, no steering.

The harness tracks write/validation counts, changed paths, and validation
state.  Assistant text is never inspected.  No counters are compared against
literals.  No steering, nudging, blocking, or inventory locking exists here.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from aura.conversation.validation_truth import validation_payload_passed
from aura.conversation.worker_flow_helpers import (
    TARGETED_READ_TOOLS,
    VALIDATION_TOOLS,
    WRITE_TOOLS,
    _parse_payload,
    _tool_paths,
    _tool_call_name_args,
    _write_was_applied,
)


@dataclass(frozen=True)
class ChangedFileClassification:
    paths: tuple[str, ...] = ()
    has_python: bool = False
    has_source: bool = False
    has_docs: bool = False
    has_other: bool = False

    @property
    def docs_only(self) -> bool:
        return bool(
            self.paths
            and self.has_docs
            and not self.has_python
            and not self.has_source
            and not self.has_other
        )


class WorkerFlowPhase(str, enum.Enum):
    orienting = "orienting"
    editing = "editing"
    validating = "validating"
    done = "done"


PYTHON_SOURCE_EXTENSIONS: frozenset[str] = frozenset({".py", ".pyw"})
SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx",
        ".css", ".scss", ".html", ".htm",
        ".go", ".rs", ".java", ".cs", ".c", ".h", ".cpp", ".hpp",
        ".sh", ".ps1",
    }
)
DOC_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".markdown", ".mdown", ".rst", ".txt", ".adoc"}
)
DOC_PATH_PREFIXES: tuple[str, ...] = (
    "docs/", "doc/", "documentation/",
)
DOC_FILENAMES: frozenset[str] = frozenset(
    {
        "readme", "changelog", "changes", "license",
        "notice", "contributing", "authors",
    }
)


@dataclass
class WorkerFlowState:
    phase: WorkerFlowPhase = WorkerFlowPhase.orienting
    write_intents: int = 0
    write_actions: int = 0
    validation_intents: int = 0
    validation_actions: int = 0
    validation_required_before_final: bool = False
    changed_paths: set[str] = field(default_factory=set)


class WorkerFlowHarness:
    """Honest write/validation accounting.

    The public methods tolerate malformed inputs and only mutate local state.
    The harness has no fatal or blocking outcome — it merely records what the
    Worker actually did.
    """

    def __init__(
        self,
        state: WorkerFlowState | None = None,
    ) -> None:
        self.state = state or WorkerFlowState()

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

    def requires_validation_before_final(self) -> bool:
        return bool(self.state.validation_required_before_final)

    def changed_file_classification(self) -> ChangedFileClassification:
        return classify_changed_files(self.state.changed_paths)

    def validation_required_text(self) -> str:
        classification = self.changed_file_classification()
        if classification.docs_only:
            return WORKER_FLOW_DOCS_VALIDATION_REQUIRED_TEXT
        return WORKER_FLOW_VALIDATION_REQUIRED_TEXT

    def mark_validation_satisfied(self) -> None:
        self.state.validation_required_before_final = False

    def observe_assistant_message(self, full_message: dict[str, Any] | str | None) -> None:
        """Observe tool calls within an assistant message (no text surveillance)."""
        if isinstance(full_message, dict):
            for tool_call in full_message.get("tool_calls") or []:
                name, args = _tool_call_name_args(tool_call)
                if name:
                    self._observe_tool_call_evidence(name, args)

    def observe_tool_call(self, name: str, args: dict[str, Any] | None = None) -> None:
        args = args if isinstance(args, dict) else {}
        self._observe_tool_call_evidence(name, args)

        if name in TARGETED_READ_TOOLS:
            return

        if name in WRITE_TOOLS:
            self.state.write_intents += 1
            self._advance_to(WorkerFlowPhase.editing)
            return

        if name in VALIDATION_TOOLS:
            self.state.validation_intents += 1

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

        if name in WRITE_TOOLS:
            if _write_was_applied(name, ok, payload):
                self.state.write_actions += 1
                for path in _tool_paths(name, args, payload):
                    self.state.changed_paths.add(self._normalize_path(path))
                self.state.validation_required_before_final = True
                self._advance_to(WorkerFlowPhase.editing)
            return

        if name in VALIDATION_TOOLS:
            self.state.validation_actions += 1
            self._advance_to(WorkerFlowPhase.validating)
            if validation_payload_passed(payload):
                self.mark_validation_satisfied()

    def _observe_tool_call_evidence(self, name: str, args: dict[str, Any]) -> None:
        pass  # Tool call observation no longer drives inventory or steering.

    def _advance_to(self, phase: WorkerFlowPhase) -> None:
        self.state.phase = phase

    def _normalize_path(self, path: str) -> str:
        normalized = str(path).strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized


WORKER_FLOW_VALIDATION_REQUIRED_TEXT = (
    "Worker Flow: files were changed and validation has not run yet. Run the "
    "focused validation command now. Do not summarize or plan. Use "
    "run_terminal_command with the smallest relevant py_compile or pytest "
    "command, then finish only after it passes."
)

WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT = (
    "Worker Flow zero-work recovery: use the context already gathered. "
    "If this bounded WorkArtifact item is already satisfied, do not invent a "
    "cosmetic edit. Re-run or cite focused validation and finish with a concrete "
    "receipt. Make the next smallest safe edit now, or return a real blocker "
    "with the exact missing fact, file/path, permission/tool failure, or "
    "planner-resolution question."
)

WORKER_FLOW_DOCS_VALIDATION_REQUIRED_TEXT = (
    "Worker Flow: documentation/text files were changed and validation has not "
    "run yet. Run a docs-appropriate validation command now if one is available "
    "or explicitly requested, such as a docs build/check or python -m compileall "
    "docs/ when that is the accepted project check. Do not run Python AST or "
    "py_compile checks against markdown/text files. Then finish only after it "
    "passes, or state that no Python/source files changed and no docs-specific "
    "command is available."
)


def classify_changed_files(
    paths: set[str] | list[str] | tuple[str, ...],
) -> ChangedFileClassification:
    normalized = tuple(
        dict.fromkeys(
            path
            for raw in paths
            if (path := _normalize_changed_path(raw))
        )
    )
    has_python = False
    has_source = False
    has_docs = False
    has_other = False
    for path in normalized:
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in PYTHON_SOURCE_EXTENSIONS:
            has_python = True
            has_source = True
        elif _is_docs_path(path):
            has_docs = True
        elif suffix in SOURCE_EXTENSIONS:
            has_source = True
        else:
            has_other = True
    return ChangedFileClassification(
        paths=normalized,
        has_python=has_python,
        has_source=has_source,
        has_docs=has_docs,
        has_other=has_other,
    )


def _normalize_changed_path(path: object) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _is_docs_path(path: str) -> bool:
    normalized = _normalize_changed_path(path).lower()
    if any(normalized.startswith(prefix) for prefix in DOC_PATH_PREFIXES):
        return True
    pure = PurePosixPath(normalized)
    if pure.suffix in DOC_EXTENSIONS:
        return True
    return pure.name in DOC_FILENAMES


__all__ = [
    "ChangedFileClassification",
    "WORKER_FLOW_DOCS_VALIDATION_REQUIRED_TEXT",
    "WORKER_FLOW_VALIDATION_REQUIRED_TEXT",
    "WORKER_FLOW_ZERO_WORK_RECOVERY_TEXT",
    "WorkerFlowHarness",
    "WorkerFlowPhase",
    "WorkerFlowState",
    "classify_changed_files",
]
