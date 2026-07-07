"""Deterministic Worker edit strategy selection and retry bookkeeping."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from aura.conversation.path_utils import normalize_worker_path


class EditMode(str, Enum):
    PATCH = "patch"
    WHOLE_FILE = "whole_file"
    FOCUSED_REPAIR = "focused_repair"


PATCH_TOOL_NAMES = frozenset(
    {
        "patch_file",
        "edit_file",
        "edit_symbol",
        "edit_line_range",
        "apply_edit_transaction",
    }
)


@dataclass(frozen=True)
class FileEditProfile:
    path: str
    exists: bool = False
    readable: bool = False
    char_count: int = 0
    line_count: int = 0
    escape_score: int = 0
    escape_density: float = 0.0
    whole_file_allowed: bool = False
    reason: str = ""

    @classmethod
    def from_content(
        cls,
        path: str,
        content: str,
        *,
        exists: bool = True,
        readable: bool = True,
    ) -> "FileEditProfile":
        normalized_path = normalize_worker_path(path)
        char_count = len(content)
        line_count = content.count("\n") + (1 if content else 0)
        escape_score = _escape_score(content)
        escape_density = escape_score / max(char_count, 1)
        small_file = char_count <= 12_000 and line_count <= 240
        escape_heavy = (
            char_count <= 32_000
            and line_count <= 500
            and (escape_score >= 12 or escape_density >= 0.025)
        )
        whole_file_allowed = small_file or escape_heavy
        if small_file and escape_heavy:
            reason = "small_escape_heavy_file"
        elif small_file:
            reason = "small_file"
        elif escape_heavy:
            reason = "escape_heavy_file"
        else:
            reason = "large_or_low_escape_file"
        return cls(
            path=normalized_path,
            exists=exists,
            readable=readable,
            char_count=char_count,
            line_count=line_count,
            escape_score=escape_score,
            escape_density=escape_density,
            whole_file_allowed=whole_file_allowed,
            reason=reason,
        )

    @classmethod
    def unknown(cls, path: str, *, exists: bool = False, reason: str = "unknown") -> "FileEditProfile":
        return cls(
            path=normalize_worker_path(path),
            exists=exists,
            readable=False,
            whole_file_allowed=not exists,
            reason=reason,
        )


@dataclass(frozen=True)
class EditFailureRecord:
    mode: EditMode
    path: str
    failure_class: str
    shape: str = ""
    error: str = ""


@dataclass(frozen=True)
class EditStrategyDecision:
    path: str
    failure_class: str
    error: str
    suggested_next_tool: str
    suggested_next_action: str
    recoverable: bool
    next_mode: EditMode | None = None
    attempted_mode: EditMode | None = None
    repair_context: dict[str, Any] = field(default_factory=dict)

    @property
    def stop(self) -> bool:
        return not self.recoverable and self.next_mode is None


@dataclass
class EditRetryLedger:
    records: list[EditFailureRecord] = field(default_factory=list)

    def record_failure(
        self,
        *,
        mode: EditMode | str | None,
        path: str,
        failure_class: str,
        shape: str = "",
        error: str = "",
    ) -> None:
        edit_mode = _coerce_mode(mode)
        normalized_path = normalize_worker_path(path)
        normalized_failure = str(failure_class or "").strip()
        if edit_mode is None or not normalized_path or not normalized_failure:
            return
        self.records.append(
            EditFailureRecord(
                mode=edit_mode,
                path=normalized_path,
                failure_class=normalized_failure,
                shape=str(shape or ""),
                error=str(error or "")[:2_000],
            )
        )

    def clear_path(self, path: str) -> None:
        normalized_path = normalize_worker_path(path)
        self.records = [
            record for record in self.records if record.path != normalized_path
        ]

    def failures_for(self, path: str) -> list[EditFailureRecord]:
        normalized_path = normalize_worker_path(path)
        return [record for record in self.records if record.path == normalized_path]

    def latest_failure(self, path: str) -> EditFailureRecord | None:
        normalized_path = normalize_worker_path(path)
        for record in reversed(self.records):
            if record.path == normalized_path:
                return record
        return None

    def next_mode(
        self,
        path: str,
        profile: FileEditProfile | None = None,
    ) -> EditMode | None:
        latest = self.latest_failure(path)
        if latest is None:
            return EditMode.PATCH
        if latest.mode == EditMode.PATCH:
            return EditMode.FOCUSED_REPAIR
        if latest.mode == EditMode.FOCUSED_REPAIR:
            if profile is not None and profile.whole_file_allowed:
                return EditMode.WHOLE_FILE
            return None
        return None

    def mode_for_tool_result(
        self,
        *,
        name: str,
        args: dict[str, Any],
        path: str,
        profile: FileEditProfile | None = None,
    ) -> EditMode | None:
        physical_mode = mode_for_tool_call(name, args)
        if physical_mode == EditMode.PATCH and name == "patch_file":
            if self.next_mode(path, profile) == EditMode.FOCUSED_REPAIR:
                return EditMode.FOCUSED_REPAIR
        return physical_mode


def select_edit_mode(
    path: str,
    content: str = "",
    *,
    ledger: EditRetryLedger | None = None,
    profile: FileEditProfile | None = None,
) -> EditMode | None:
    file_profile = profile or FileEditProfile.from_content(path, content)
    if ledger is None:
        return EditMode.PATCH
    return ledger.next_mode(path, file_profile)


def mode_for_tool_call(name: str, args: dict[str, Any]) -> EditMode | None:
    if name == "write_file":
        if args.get("full_replace_existing") is True:
            return EditMode.WHOLE_FILE
        return None
    if name in PATCH_TOOL_NAMES:
        return EditMode.PATCH
    return None


def strategy_decision_for_attempt(
    *,
    ledger: EditRetryLedger,
    name: str,
    args: dict[str, Any],
    path: str,
    profile: FileEditProfile,
) -> EditStrategyDecision | None:
    attempted_mode = mode_for_tool_call(name, args)
    if attempted_mode is None or not path:
        return None

    if attempted_mode == EditMode.PATCH and name == "patch_file":
        if ledger.next_mode(path, profile) == EditMode.FOCUSED_REPAIR:
            attempted_mode = EditMode.FOCUSED_REPAIR

    latest = ledger.latest_failure(path)
    if latest is None:
        if (
            attempted_mode == EditMode.WHOLE_FILE
            and profile.exists
            and not profile.whole_file_allowed
        ):
            return _decision(
                path=path,
                failure_class="edit_strategy_whole_file_not_allowed",
                error=(
                    "Whole-file replacement is not selected for this existing file. "
                    "Normal existing-file edits should use patch_file/edit unless the file is small "
                    "or escape-heavy enough for deterministic replacement."
                ),
                attempted_mode=attempted_mode,
                next_mode=EditMode.PATCH,
                recoverable=True,
                latest=latest,
                profile=profile,
            )
        return None

    next_mode = ledger.next_mode(path, profile)
    if next_mode is None:
        return _decision(
            path=path,
            failure_class="edit_strategy_exhausted",
            error=(
                "Edit strategy is exhausted for this file. The Worker already used the "
                f"{latest.mode.value} strategy after {latest.failure_class}; stop and report the exact blocker."
            ),
            attempted_mode=attempted_mode,
            next_mode=None,
            recoverable=False,
            latest=latest,
            profile=profile,
        )

    if attempted_mode != next_mode:
        return _decision(
            path=path,
            failure_class="edit_strategy_switch_required",
            error=(
                "Do not repeat the previous edit strategy for this file. "
                f"Previous failure: {latest.failure_class}. "
                f"Switch to {next_mode.value}."
            ),
            attempted_mode=attempted_mode,
            next_mode=next_mode,
            recoverable=True,
            latest=latest,
            profile=profile,
        )

    if (
        attempted_mode == EditMode.WHOLE_FILE
        and profile.exists
        and not profile.whole_file_allowed
    ):
        return _decision(
            path=path,
            failure_class="edit_strategy_whole_file_not_allowed",
            error=(
                "Whole-file replacement is not safe for this existing file. "
                "Stop and report the focused blocker instead of rewriting the file."
            ),
            attempted_mode=attempted_mode,
            next_mode=None,
            recoverable=False,
            latest=latest,
            profile=profile,
        )

    return None


def load_file_edit_profile(workspace_root: str | Path, path: str) -> FileEditProfile:
    normalized_path = normalize_worker_path(path)
    if not normalized_path:
        return FileEditProfile.unknown(path, reason="missing_path")
    try:
        root = Path(workspace_root).resolve()
        target = Path(normalized_path)
        if not target.is_absolute():
            target = root / target
        resolved = target.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return FileEditProfile.unknown(normalized_path, exists=True, reason="outside_workspace")

    if not resolved.exists():
        return FileEditProfile.unknown(normalized_path, exists=False, reason="new_file")
    if not resolved.is_file():
        return FileEditProfile.unknown(normalized_path, exists=True, reason="not_regular_file")
    try:
        content = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return FileEditProfile.unknown(normalized_path, exists=True, reason="unreadable")
    return FileEditProfile.from_content(normalized_path, content, exists=True, readable=True)


def _decision(
    *,
    path: str,
    failure_class: str,
    error: str,
    attempted_mode: EditMode,
    next_mode: EditMode | None,
    recoverable: bool,
    latest: EditFailureRecord | None,
    profile: FileEditProfile,
) -> EditStrategyDecision:
    suggested_tool, suggested_action = _suggestion_for_mode(next_mode, path, profile)
    repair_context: dict[str, Any] = {
        "path": normalize_worker_path(path),
        "file": normalize_worker_path(path),
        "attempted_mode": attempted_mode.value,
        "next_mode": next_mode.value if next_mode else "none",
        "file_profile": {
            "reason": profile.reason,
            "char_count": profile.char_count,
            "line_count": profile.line_count,
            "escape_score": profile.escape_score,
            "escape_density": profile.escape_density,
            "whole_file_allowed": profile.whole_file_allowed,
        },
    }
    if latest is not None:
        repair_context["last_failure"] = {
            "mode": latest.mode.value,
            "failure_class": latest.failure_class,
            "shape": latest.shape,
            "error": latest.error,
        }
    return EditStrategyDecision(
        path=normalize_worker_path(path),
        failure_class=failure_class,
        error=error,
        suggested_next_tool=suggested_tool,
        suggested_next_action=suggested_action,
        recoverable=recoverable,
        next_mode=next_mode,
        attempted_mode=attempted_mode,
        repair_context=repair_context,
    )


def _suggestion_for_mode(
    mode: EditMode | None,
    path: str,
    profile: FileEditProfile,
) -> tuple[str, str]:
    if mode == EditMode.PATCH:
        return (
            "patch_file",
            "Read the current file, then apply one targeted patch_file/edit with exact current text.",
        )
    if mode == EditMode.FOCUSED_REPAIR:
        return (
            "patch_file",
            (
                "Make one focused repair on "
                f"{normalize_worker_path(path)} using only the changed file and exact failure context."
            ),
        )
    if mode == EditMode.WHOLE_FILE:
        return (
            "write_file",
            (
                "Replace the whole file with write_file using full_replace_existing=true "
                "and a replacement_reason. This is allowed because "
                f"{profile.reason}."
            ),
        )
    return (
        "none",
        "Stop and summarize the exact edit blocker; do not call more edit tools for this file.",
    )


def _escape_score(content: str) -> int:
    backslashes = content.count("\\")
    escaped_quotes = content.count('\\"') + content.count("\\'")
    escaped_newlines = content.count("\\n") + content.count("\\r") + content.count("\\t")
    windows_paths = len(re.findall(r"(?:[A-Za-z]:\\|\\\\|\\[A-Za-z0-9_.-])", content))
    regex_escape_chars = set(
        "AbBdDsSwWZnrtt.[](){}/+*?|^$-"
    )
    regex_escapes = sum(
        1
        for index, char in enumerate(content[:-1])
        if char == "\\" and content[index + 1] in regex_escape_chars
    )
    return backslashes + escaped_quotes + escaped_newlines + windows_paths + regex_escapes


def _coerce_mode(mode: EditMode | str | None) -> EditMode | None:
    if isinstance(mode, EditMode):
        return mode
    if isinstance(mode, str) and mode:
        try:
            return EditMode(mode)
        except ValueError:
            return None
    return None


__all__ = [
    "EditFailureRecord",
    "EditMode",
    "EditRetryLedger",
    "EditStrategyDecision",
    "FileEditProfile",
    "load_file_edit_profile",
    "mode_for_tool_call",
    "select_edit_mode",
    "strategy_decision_for_attempt",
]
