"""Planner -> Worker dispatch types.

The planner manager calls `dispatch_to_worker` (a tool) when it has enough
information to delegate a code change. Args are validated here, the manager
emits a WorkerDispatchRequested event to the GUI, then calls a
DispatchCallback to actually run the worker; the result is fed back to the
planner as the tool_result for that call.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from aura.conversation._dispatch_helpers import (
    _extract_validation_commands,
    _none_or_str,
    _require_list_str,
    _str_list_items,
    _string_dict_list,
    _string_list,
    _target_region_list,
)
from aura.conversation.project_profile import ProjectProfile
from aura.conversation.task_shape import TaskShape, infer_task_shape, unknown_task_shape
from aura.conversation.worker_outcome import (
    WorkerOutcomeStatus,
    normalize_outcome_status,
)


@dataclass
class ExplicitSpecContract:
    """Formal contract between Planner and Worker.

    Captures what the Worker must produce and must not do.
    Populated from the Planner's dispatch fields.
    """
    expected_public_symbols: list[str] = field(default_factory=list)
    expected_dataclass_fields: dict[str, list[str]] = field(default_factory=dict)
    forbidden_public_methods: list[str] = field(default_factory=list)
    forbidden_calls: list[str] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)


@dataclass
class WorkerDispatchRequest:
    goal: str
    files: list[str]
    spec: str
    acceptance: str
    summary: str = ""
    run_command: str = ""
    target_regions: list[dict[str, Any]] = field(default_factory=list)
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
    task_shape: TaskShape | None = None
    artifact_id: str = ""
    artifact_item_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "goal": self.goal,
            "files": list(self.files),
            "target_regions": _target_region_list(self.target_regions),
            "spec": self.spec,
            "acceptance": self.acceptance,
            "summary": self.summary,
            "run_command": self.run_command,
            "allowed_responsibilities": list(self.allowed_responsibilities),            "forbidden_responsibilities": list(self.forbidden_responsibilities),
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
        if self.task_shape is not None:
            payload["task_shape"] = self.task_shape.to_dict()
        if self.artifact_id:
            payload["artifact_id"] = self.artifact_id
        if self.artifact_item_id:
            payload["artifact_item_id"] = self.artifact_item_id
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerDispatchRequest":
        if not isinstance(data, dict):
            data = {}
        files = data.get("files") or []
        if not isinstance(files, list):
            files = []
        task_shape = None
        if "task_shape" in data:
            task_shape = TaskShape.from_dict(data.get("task_shape"))
        return cls(
            goal=str(data.get("goal", "")),
            files=[str(f) for f in files],
            target_regions=_target_region_list(data.get("target_regions")),
            spec=str(data.get("spec", "")),
            acceptance=str(data.get("acceptance", "")),
            summary=str(data.get("summary", "")),
            run_command=str(data.get("run_command", "")),
            allowed_responsibilities=_string_list(data.get("allowed_responsibilities")),            forbidden_responsibilities=_string_list(data.get("forbidden_responsibilities")),
            required_outputs=_string_list(data.get("required_outputs")),
            validation_commands=_string_list(data.get("validation_commands")),
            risk_notes=_string_list(data.get("risk_notes")),
            non_goals=_string_list(data.get("non_goals")),
            expected_public_symbols=_string_list(data.get("expected_public_symbols")),
            expected_dataclass_fields=_string_dict_list(data.get("expected_dataclass_fields")),
            forbidden_public_methods=_string_list(data.get("forbidden_public_methods")),
            forbidden_calls=_string_list(data.get("forbidden_calls")),
            contract=ExplicitSpecContract.from_dict(data["contract"]) if data.get("contract") else None,
            task_shape=task_shape,
            artifact_id=str(data.get("artifact_id", "")),
            artifact_item_id=str(data.get("artifact_item_id", "")),
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
    status: str | None = None
    completed: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    validation: str | None = None
    suggested_next_spec: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    mismatch: WorkerMismatch | None = None

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
        extras = dict(self.extras)
        if self.mismatch is not None:
            extras.setdefault("mismatch_detected", True)
            extras.setdefault("mismatch_kind", self.mismatch.kind)
            extras.setdefault("mismatch_question", self.mismatch.question_for_planner)
        if extras:
            payload["extras"] = extras
        if self.mismatch is not None:
            payload["mismatch"] = self.mismatch.to_dict()
        if self.status is not None:
            payload["status"] = self.status
        return payload

    @classmethod
    def from_tool_payload(cls, data: dict[str, Any]) -> "WorkerDispatchResult":
        """Restore a dispatch result from a planner tool payload."""
        status = normalize_outcome_status(data.get("status"))
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
            status=status,
            completed=_string_list(data.get("completed")),
            remaining=_string_list(data.get("remaining")),
            modified_files=_string_list(data.get("modified_files")),
            validation=str(data["validation"]) if data.get("validation") is not None else None,
            suggested_next_spec=(
                str(data["suggested_next_spec"]) if data.get("suggested_next_spec") is not None else None
            ),
            extras=data.get("extras") if isinstance(data.get("extras"), dict) else {},
            mismatch=WorkerMismatch.from_dict(data.get("mismatch")),
        )


@dataclass(frozen=True)
class WorkerMismatch:
    """Structured mismatch report: Worker discovered Planner handoff conflicts with repo reality."""

    kind: str
    file_paths: list[str]
    requested: str
    observed: str
    worker_recommendation: str
    question_for_planner: str

    # Supported kind values
    MISSING_SYMBOL = "missing_symbol"
    SCHEMA_MISMATCH = "schema_mismatch"
    CONFLICTING_SPEC = "conflicting_spec"
    AMBIGUOUS_PRODUCT_DECISION = "ambiguous_product_decision"
    REPEATED_EDIT_FAILURE = "repeated_edit_failure"
    VALIDATION_UNCLEAR = "validation_unclear"

    _KINDS = (
        MISSING_SYMBOL,
        SCHEMA_MISMATCH,
        CONFLICTING_SPEC,
        AMBIGUOUS_PRODUCT_DECISION,
        REPEATED_EDIT_FAILURE,
        VALIDATION_UNCLEAR,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "file_paths": list(self.file_paths),
            "requested": self.requested,
            "observed": self.observed,
            "worker_recommendation": self.worker_recommendation,
            "question_for_planner": self.question_for_planner,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkerMismatch | None":
        if not isinstance(raw, dict):
            return None
        return cls(
            kind=str(raw.get("kind", "")),
            file_paths=_require_list_str(raw.get("file_paths", []), "file_paths"),
            requested=str(raw.get("requested", "")),
            observed=str(raw.get("observed", "")),
            worker_recommendation=str(raw.get("worker_recommendation", "")),
            question_for_planner=str(raw.get("question_for_planner", "")),
        )


def infer_outcome_status(result: WorkerDispatchResult) -> str:
    """Infer an outcome status from legacy boolean fields when no explicit status is set."""
    explicit = normalize_outcome_status(result.status)
    if explicit is not None:
        return explicit
    if result.cancelled:
        return WorkerOutcomeStatus.cancelled.value
    if result.mismatch is not None:
        return WorkerOutcomeStatus.harness_error.value
    if result.extras.get("mismatch_detected"):
        return WorkerOutcomeStatus.harness_error.value
    if not result.ok:
        extras = result.extras if isinstance(result.extras, dict) else {}
        errors = extras.get("errors") if isinstance(extras.get("errors"), list) else []
        validation_results = (
            extras.get("validation_results")
            if isinstance(extras.get("validation_results"), list)
            else []
        )
        if any(
            isinstance(item, dict) and item.get("exit_code") not in (0, None)
            for item in validation_results
        ) or result.validation:
            return WorkerOutcomeStatus.validation_failed.value
        if extras.get("unrecovered_not_applied_writes") or extras.get("not_applied_writes"):
            return WorkerOutcomeStatus.edit_mechanics_blocked.value
        internal_signals = [
            extras.get("worker_internal_error"),
            extras.get("internal_error"),
            bool(extras.get("api_errors")),
            result.summary,
            *errors,
        ]
        if any(_looks_like_harness_error(signal) for signal in internal_signals):
            return WorkerOutcomeStatus.harness_error.value
        return WorkerOutcomeStatus.harness_error.value
    if result.needs_followup:
        return WorkerOutcomeStatus.completed_with_caveats.value
    return WorkerOutcomeStatus.completed.value


def _looks_like_harness_error(value: Any) -> bool:
    if value is True:
        return True
    if not value:
        return False
    text = str(value).lower()
    return any(
        marker in text
        for marker in (
            "harness error",
            "internal worker exception",
            "internal worker dispatch exception",
            "worker_internal_error",
            "api error",
        )
    )


@dataclass
class WorkerTaskSpec:
    """Structured task artifact — a richer, bounded handoff for the Worker.

    All fields are optional/defaulted so existing dispatch_to_worker calls
    continue working. Future Planners can populate richer fields directly.
    """
    goal: str = ""
    files: list[str] = field(default_factory=list)
    target_regions: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    builder_note: str = ""
    acceptance: str = ""
    run_command: str = ""
    allowed_responsibilities: list[str] = field(default_factory=list)
    forbidden_responsibilities: list[str] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    contract: ExplicitSpecContract | None = None
    project_profile: ProjectProfile | None = None
    task_shape: TaskShape | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "goal": self.goal,
            "files": list(self.files),
            "target_regions": _target_region_list(self.target_regions),
            "summary": self.summary,
            "builder_note": self.builder_note,
            "acceptance": self.acceptance,
            "run_command": self.run_command,
            "allowed_responsibilities": list(self.allowed_responsibilities),
            "forbidden_responsibilities": list(self.forbidden_responsibilities),
            "required_outputs": list(self.required_outputs),
            "validation_commands": list(self.validation_commands),
            "risk_notes": list(self.risk_notes),
            "non_goals": list(self.non_goals),
        }
        if self.task_shape is not None:
            result["task_shape"] = self.task_shape.to_dict()
        if self.project_profile is not None:
            result["project_profile"] = {
                "workspace_root": self.project_profile.workspace_root,
                "project_types": list(self.project_profile.project_types),
                "manifests": list(self.project_profile.manifests),
                "lockfiles": list(self.project_profile.lockfiles),
                "package_manager": self.project_profile.package_manager,
                "has_venv": self.project_profile.has_venv,
                "python_venv_path": self.project_profile.python_venv_path,
                "python_executable": self.project_profile.python_executable,
                "declared_dependencies": list(self.project_profile.declared_dependencies),
                "validation_commands": list(self.project_profile.validation_commands),
                "node_scripts": [list(s) for s in self.project_profile.node_scripts],
            }
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerTaskSpec":
        def _str_list(key: str) -> list[str]:
            raw = data.get(key)
            if not isinstance(raw, list):
                return []
            return [str(v) for v in raw]

        project_profile: ProjectProfile | None = None
        raw_profile = data.get("project_profile")
        if isinstance(raw_profile, dict):
            project_profile = ProjectProfile(
                workspace_root=str(raw_profile.get("workspace_root", "")),
                project_types=tuple(_str_list_items(raw_profile, "project_types")),
                manifests=tuple(_str_list_items(raw_profile, "manifests")),
                lockfiles=tuple(_str_list_items(raw_profile, "lockfiles")),
                package_manager=_none_or_str(raw_profile.get("package_manager")),
                has_venv=bool(raw_profile.get("has_venv", False)),
                python_venv_path=_none_or_str(raw_profile.get("python_venv_path")),
                python_executable=_none_or_str(raw_profile.get("python_executable")),
                declared_dependencies=tuple(_str_list_items(raw_profile, "declared_dependencies")),
                validation_commands=tuple(_str_list_items(raw_profile, "validation_commands")),
                node_scripts=tuple(
                    (str(s[0]), str(s[1]))
                    for s in raw_profile.get("node_scripts", [])
                    if isinstance(s, list) and len(s) == 2
                ),
            )

        return cls(
            goal=str(data.get("goal", "")),
            files=_str_list("files"),
            target_regions=_target_region_list(data.get("target_regions")),
            summary=str(data.get("summary", "")),
            builder_note=str(data.get("builder_note", "")),
            acceptance=str(data.get("acceptance", "")),
            run_command=str(data.get("run_command", "")),
            allowed_responsibilities=_str_list("allowed_responsibilities"),
            forbidden_responsibilities=_str_list("forbidden_responsibilities"),
            required_outputs=_str_list("required_outputs"),
            validation_commands=_str_list("validation_commands"),
            risk_notes=_str_list("risk_notes"),
            non_goals=_str_list("non_goals"),
            contract=ExplicitSpecContract.from_dict(data["contract"]) if data.get("contract") else None,
            project_profile=project_profile,
            task_shape=TaskShape.from_dict(data.get("task_shape")) if "task_shape" in data else None,
        )




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

    task_shape = req.task_shape
    if task_shape is None:
        started = time.perf_counter()
        try:
            task_shape = infer_task_shape(
                goal=req.goal,
                spec=req.spec,
                files=req.files,
                user_text=req.summary,
            )
        except Exception:
            task_shape = unknown_task_shape(req.goal or req.summary or req.spec)
        setattr(task_shape, "_task_shape_ms", round((time.perf_counter() - started) * 1000, 3))
    else:
        setattr(task_shape, "_task_shape_ms", 0.0)

    return WorkerTaskSpec(
        goal=req.goal,
        files=list(req.files),
        target_regions=_target_region_list(req.target_regions),
        summary=req.summary,
        builder_note=req.spec,
        acceptance=req.acceptance,
        validation_commands=validation_commands,
        non_goals=non_goals,
        allowed_responsibilities=list(req.allowed_responsibilities),
        forbidden_responsibilities=list(req.forbidden_responsibilities),
        required_outputs=list(req.required_outputs),
        risk_notes=list(req.risk_notes),
        run_command=req.run_command,
        contract=contract,
        project_profile=None,
        task_shape=task_shape,
    )




DispatchCallback = Callable[[str, WorkerDispatchRequest], WorkerDispatchResult]
"""Called from the planner's worker thread.

Args: (tool_call_id, request). Blocks until the GUI/user has approved or
cancelled the dispatch and (if approved) the worker manager has finished.
"""


__all__ = [
    "WorkerDispatchRequest",
    "WorkerDispatchResult",
    "WorkerMismatch",
    "WorkerOutcomeStatus",
    "WorkerTaskSpec",
    "TaskShape",
    "DispatchCallback",
    "infer_outcome_status",
    "normalize_outcome_status",
    "normalize_worker_task",
]
