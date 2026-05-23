"""Planner -> Worker dispatch types.

The planner manager calls `dispatch_to_worker` (a tool) when it has enough
information to delegate a code change. Args are validated here, the manager
emits a WorkerDispatchRequested event to the GUI, then calls a
DispatchCallback to actually run the worker; the result is fed back to the
planner as the tool_result for that call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from aura.craft.types import ExplicitSpecContract


@dataclass
class WorkerDispatchRequest:
    goal: str
    files: list[str]
    spec: str
    acceptance: str
    summary: str = ""
    allowed_responsibilities: list[str] = field(default_factory=list)
    forbidden_responsibilities: list[str] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    expected_public_symbols: list[str] = field(default_factory=list)
    expected_dataclass_fields: dict[str, list[str]] = field(default_factory=dict)
    forbidden_public_methods: list[str] = field(default_factory=list)
    forbidden_calls: list[str] = field(default_factory=list)
    contract: ExplicitSpecContract | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "files": list(self.files),
            "spec": self.spec,
            "acceptance": self.acceptance,
            "summary": self.summary,
            "allowed_responsibilities": list(self.allowed_responsibilities),
            "forbidden_responsibilities": list(self.forbidden_responsibilities),
            "required_outputs": list(self.required_outputs),
            "validation_commands": list(self.validation_commands),
            "risk_notes": list(self.risk_notes),
            "non_goals": list(self.non_goals),
            "expected_public_symbols": list(self.expected_public_symbols),
            "expected_dataclass_fields": dict(self.expected_dataclass_fields),
            "forbidden_public_methods": list(self.forbidden_public_methods),
            "forbidden_calls": list(self.forbidden_calls),
            "contract": self.contract.to_dict() if self.contract else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerDispatchRequest":
        files = data.get("files") or []
        if not isinstance(files, list):
            files = []
        return cls(
            goal=str(data.get("goal", "")),
            files=[str(f) for f in files],
            spec=str(data.get("spec", "")),
            acceptance=str(data.get("acceptance", "")),
            summary=str(data.get("summary", "")),
            allowed_responsibilities=_string_list(data.get("allowed_responsibilities")),
            forbidden_responsibilities=_string_list(data.get("forbidden_responsibilities")),
            required_outputs=_string_list(data.get("required_outputs")),
            validation_commands=_string_list(data.get("validation_commands")),
            risk_notes=_string_list(data.get("risk_notes")),
            non_goals=_string_list(data.get("non_goals")),
            expected_public_symbols=_string_list(data.get("expected_public_symbols")),
            expected_dataclass_fields=_string_dict_list(data.get("expected_dataclass_fields")),
            forbidden_public_methods=_string_list(data.get("forbidden_public_methods")),
            forbidden_calls=_string_list(data.get("forbidden_calls")),
            contract=ExplicitSpecContract.from_dict(data["contract"]) if data.get("contract") else None,
        )


@dataclass
class WorkerDispatchResult:
    ok: bool
    summary: str
    cancelled: bool = False
    needs_followup: bool = False
    phase_boundary: bool = False
    followup_reason: str | None = None
    recoverable: bool = False
    completed: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    validation: str | None = None
    suggested_next_spec: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_tool_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "cancelled": self.cancelled,
            "summary": self.summary,
        }
        if self.needs_followup:
            payload["needs_followup"] = self.needs_followup
        if self.phase_boundary:
            payload["phase_boundary"] = self.phase_boundary
        if self.followup_reason is not None:
            payload["followup_reason"] = self.followup_reason
        if self.recoverable:
            payload["recoverable"] = self.recoverable
        if self.completed:
            payload["completed"] = list(self.completed)
        if self.remaining:
            payload["remaining"] = list(self.remaining)
        if self.modified_files:
            payload["modified_files"] = list(self.modified_files)
        if self.validation is not None:
            payload["validation"] = self.validation
        if self.suggested_next_spec is not None:
            payload["suggested_next_spec"] = self.suggested_next_spec
        if self.extras:
            payload["extras"] = self.extras
        return payload

    @classmethod
    def from_tool_payload(cls, data: dict[str, Any]) -> "WorkerDispatchResult":
        """Restore a dispatch result from a planner tool payload."""
        return cls(
            ok=bool(data.get("ok", False)),
            summary=str(data.get("summary", "")),
            cancelled=bool(data.get("cancelled", False)),
            needs_followup=bool(data.get("needs_followup", False)),
            phase_boundary=bool(data.get("phase_boundary", False)),
            followup_reason=(
                str(data["followup_reason"]) if data.get("followup_reason") is not None else None
            ),
            recoverable=bool(data.get("recoverable", False)),
            completed=_string_list(data.get("completed")),
            remaining=_string_list(data.get("remaining")),
            modified_files=_string_list(data.get("modified_files")),
            validation=str(data["validation"]) if data.get("validation") is not None else None,
            suggested_next_spec=(
                str(data["suggested_next_spec"]) if data.get("suggested_next_spec") is not None else None
            ),
            extras=data.get("extras") if isinstance(data.get("extras"), dict) else {},
        )


@dataclass
class WorkerTaskSpec:
    """Structured task artifact — a richer, bounded handoff for the Worker.

    All fields are optional/defaulted so existing dispatch_to_worker calls
    continue working. Future Planners can populate richer fields directly.
    """
    goal: str = ""
    files: list[str] = field(default_factory=list)
    summary: str = ""
    builder_note: str = ""
    acceptance: str = ""
    allowed_responsibilities: list[str] = field(default_factory=list)
    forbidden_responsibilities: list[str] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    contract: ExplicitSpecContract | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "files": list(self.files),
            "summary": self.summary,
            "builder_note": self.builder_note,
            "acceptance": self.acceptance,
            "allowed_responsibilities": list(self.allowed_responsibilities),
            "forbidden_responsibilities": list(self.forbidden_responsibilities),
            "required_outputs": list(self.required_outputs),
            "validation_commands": list(self.validation_commands),
            "risk_notes": list(self.risk_notes),
            "non_goals": list(self.non_goals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerTaskSpec":
        def _str_list(key: str) -> list[str]:
            raw = data.get(key)
            if not isinstance(raw, list):
                return []
            return [str(v) for v in raw]

        return cls(
            goal=str(data.get("goal", "")),
            files=_str_list("files"),
            summary=str(data.get("summary", "")),
            builder_note=str(data.get("builder_note", "")),
            acceptance=str(data.get("acceptance", "")),
            allowed_responsibilities=_str_list("allowed_responsibilities"),
            forbidden_responsibilities=_str_list("forbidden_responsibilities"),
            required_outputs=_str_list("required_outputs"),
            validation_commands=_str_list("validation_commands"),
            risk_notes=_str_list("risk_notes"),
            non_goals=_str_list("non_goals"),
            contract=ExplicitSpecContract.from_dict(data["contract"]) if data.get("contract") else None,
        )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _string_dict_list(value: Any) -> dict[str, list[str]]:
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


def _extract_validation_commands(text: str) -> list[str]:
    """Extract validation commands from acceptance text.

    Uses two strategies:
    1. Backtick-quoted commands — everything between backticks that
       starts with a known command prefix.
    2. Full-line command forms — lines that start with a known command
       prefix after stripping leading bullets/whitespace.

    Does NOT scrape partial commands from prose sentences.
    Deduplicates while preserving order.
    """
    commands: list[str] = []
    seen: set[str] = set()

    _cmd_prefix = r"(?:python -m|pytest|python|ruff|mypy|py_compile|compileall)"

    # 1. Backtick-quoted commands — capture everything between backticks.
    for m in re.finditer(
        rf"`((?:{_cmd_prefix})\s+\S[^`]*)`",
        text,
    ):
        cmd = m.group(1).strip()
        if cmd not in seen:
            seen.add(cmd)
            commands.append(cmd)

    # 2. Full-line command forms.
    for line in text.splitlines():
        stripped = line.strip()
        while stripped and stripped[0] in "-* ":
            stripped = stripped[1:].lstrip()
        if re.match(rf"{_cmd_prefix}\s", stripped):
            # Strip a single trailing sentence-ending period.
            if stripped.endswith(".") and len(stripped) > 1 and stripped[-2].isalpha():
                stripped = stripped[:-1]
            cmd = stripped
            if cmd not in seen:
                seen.add(cmd)
                commands.append(cmd)

    return commands


def normalize_worker_task(req: WorkerDispatchRequest) -> WorkerTaskSpec:
    """Convert a WorkerDispatchRequest into a structured WorkerTaskSpec.

    Preserves all existing fields. Parses obvious validation commands
    from acceptance text when possible. Leaves unknown structured fields
    empty — this is a normalization, not a full enrichment.
    """
    # validation_commands: explicit overrides, else extract from acceptance
    if req.validation_commands:
        validation_commands = list(req.validation_commands)
    else:
        validation_commands = _extract_validation_commands(req.acceptance)

    # non_goals: explicit overrides, else parse Non-Goals section from spec
    if req.non_goals:
        non_goals = list(req.non_goals)
    else:
        non_goals: list[str] = []
        if req.spec.strip():
            match = re.search(
                r"(?:#+\s*)?Non[- ]?Goals?\s*[:\.-]?\s*\n(.*?)(?=\n(?:#+\s|\n\n|\Z))",
                req.spec,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if match:
                section = match.group(1).strip()
                for line in section.splitlines():
                    line = line.strip()
                    if line and line.startswith(("-", "*")):
                        non_goals.append(line.lstrip("-* ").strip())

    contract = ExplicitSpecContract(
        expected_public_symbols=list(req.expected_public_symbols),
        expected_dataclass_fields={k: list(v) for k, v in req.expected_dataclass_fields.items()},
        forbidden_public_methods=list(req.forbidden_public_methods),
        forbidden_calls=list(req.forbidden_calls),
        required_outputs=list(req.required_outputs),
        non_goals=non_goals,
    )

    return WorkerTaskSpec(
        goal=req.goal,
        files=list(req.files),
        summary=req.summary,
        builder_note=req.spec,
        acceptance=req.acceptance,
        validation_commands=validation_commands,
        non_goals=non_goals,
        allowed_responsibilities=list(req.allowed_responsibilities),
        forbidden_responsibilities=list(req.forbidden_responsibilities),
        required_outputs=list(req.required_outputs),
        risk_notes=list(req.risk_notes),
        contract=contract,
    )


DispatchCallback = Callable[[str, WorkerDispatchRequest], WorkerDispatchResult]
"""Called from the planner's worker thread.

Args: (tool_call_id, request). Blocks until the GUI/user has approved or
cancelled the dispatch and (if approved) the worker manager has finished.
"""


__all__ = [
    "WorkerDispatchRequest",
    "WorkerDispatchResult",
    "WorkerTaskSpec",
    "DispatchCallback",
    "normalize_worker_task",
]
