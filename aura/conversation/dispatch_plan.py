"""Structured dispatch campaign and worker-step planning models.

A WorkerDispatchPlan is the durable internal campaign behind one visible
Planner -> Worker dispatch. Each WorkerStepSpec is intentionally bounded so
DispatchSession can hand the Worker one concrete work order at a time while the
user still sees one dispatch card, one Worker run, and one aggregate receipt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult


@dataclass(frozen=True)
class StepValidationPolicy:
    """Validation policy for one Worker step.

    The tier is descriptive for now. DispatchSession and later validation gates
    can use it to avoid running expensive validation on every micro-step while
    still requiring cheap proof for every step.
    """

    tier: str = "structural"
    commands: list[str] = field(default_factory=list)
    required_proofs: list[str] = field(default_factory=list)
    run_full_campaign_validation: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "commands": list(self.commands),
            "required_proofs": list(self.required_proofs),
            "run_full_campaign_validation": self.run_full_campaign_validation,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "StepValidationPolicy":
        if not isinstance(raw, dict):
            return cls()
        commands = raw.get("commands") if isinstance(raw.get("commands"), list) else []
        proofs = raw.get("required_proofs") if isinstance(raw.get("required_proofs"), list) else []
        return cls(
            tier=str(raw.get("tier") or "structural"),
            commands=[str(command) for command in commands],
            required_proofs=[str(proof) for proof in proofs],
            run_full_campaign_validation=bool(raw.get("run_full_campaign_validation", False)),
            reason=str(raw.get("reason") or ""),
        )


@dataclass(frozen=True)
class WorkerStepSpec:
    """One bounded work order for the Worker.

    This is not a second WorkerTaskSpec. WorkerTaskSpec remains the normalized
    one-pass Worker handoff. This object is the plan cursor item that can be
    converted into a bounded WorkerDispatchRequest for the active step.
    """

    id: str
    title: str
    goal: str
    spec: str = ""
    files: list[str] = field(default_factory=list)
    target_regions: list[dict[str, Any]] = field(default_factory=list)
    acceptance: str = ""
    validation_commands: list[str] = field(default_factory=list)
    required_outputs: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    expected_public_symbols: list[str] = field(default_factory=list)
    expected_dataclass_fields: dict[str, list[str]] = field(default_factory=dict)
    forbidden_calls: list[str] = field(default_factory=list)
    forbidden_public_methods: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    validation_policy: StepValidationPolicy = field(default_factory=StepValidationPolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "spec": self.spec,
            "files": list(self.files),
            "target_regions": [dict(region) for region in self.target_regions],
            "acceptance": self.acceptance,
            "validation_commands": list(self.validation_commands),
            "required_outputs": list(self.required_outputs),
            "non_goals": list(self.non_goals),
            "expected_public_symbols": list(self.expected_public_symbols),
            "expected_dataclass_fields": {
                str(name): [str(field_name) for field_name in fields]
                for name, fields in self.expected_dataclass_fields.items()
            },
            "forbidden_calls": list(self.forbidden_calls),
            "forbidden_public_methods": list(self.forbidden_public_methods),
            "risk_notes": list(self.risk_notes),
            "validation_policy": self.validation_policy.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkerStepSpec":
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            id=str(raw.get("id") or "step-1"),
            title=str(raw.get("title") or raw.get("goal") or "Worker step"),
            goal=str(raw.get("goal") or raw.get("title") or ""),
            spec=str(raw.get("spec") or ""),
            files=_str_list(raw.get("files")),
            target_regions=_dict_list(raw.get("target_regions")),
            acceptance=str(raw.get("acceptance") or ""),
            validation_commands=_str_list(raw.get("validation_commands")),
            required_outputs=_str_list(raw.get("required_outputs")),
            non_goals=_str_list(raw.get("non_goals")),
            expected_public_symbols=_str_list(raw.get("expected_public_symbols")),
            expected_dataclass_fields=_str_dict_list(raw.get("expected_dataclass_fields")),
            forbidden_calls=_str_list(raw.get("forbidden_calls")),
            forbidden_public_methods=_str_list(raw.get("forbidden_public_methods")),
            risk_notes=_str_list(raw.get("risk_notes")),
            validation_policy=StepValidationPolicy.from_dict(raw.get("validation_policy")),
        )


@dataclass(frozen=True)
class WorkerDispatchPlan:
    """Internal campaign plan behind a single visible Worker dispatch."""

    overall_goal: str
    visible_summary: str = ""
    global_files: list[str] = field(default_factory=list)
    global_non_goals: list[str] = field(default_factory=list)
    steps: list[WorkerStepSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_goal": self.overall_goal,
            "visible_summary": self.visible_summary,
            "global_files": list(self.global_files),
            "global_non_goals": list(self.global_non_goals),
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkerDispatchPlan":
        if not isinstance(raw, dict):
            raw = {}
        steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
        return cls(
            overall_goal=str(raw.get("overall_goal") or raw.get("goal") or ""),
            visible_summary=str(raw.get("visible_summary") or raw.get("summary") or ""),
            global_files=_str_list(raw.get("global_files")),
            global_non_goals=_str_list(raw.get("global_non_goals")),
            steps=[WorkerStepSpec.from_dict(step) for step in steps],
        )


@dataclass(frozen=True)
class StepResult:
    """Result for one active WorkerStepSpec."""

    step_id: str
    ok: bool
    status: str | None = None
    summary: str = ""
    modified_files: list[str] = field(default_factory=list)
    validation: str | None = None
    needs_planner_resolution: bool = False
    user_only_blocker: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step_id": self.step_id,
            "ok": self.ok,
            "summary": self.summary,
            "modified_files": list(self.modified_files),
            "needs_planner_resolution": self.needs_planner_resolution,
            "user_only_blocker": self.user_only_blocker,
            "extras": dict(self.extras),
        }
        if self.status is not None:
            payload["status"] = self.status
        if self.validation is not None:
            payload["validation"] = self.validation
        return payload

    @classmethod
    def from_worker_result(cls, step_id: str, result: WorkerDispatchResult) -> "StepResult":
        extras = result.extras if isinstance(result.extras, dict) else {}
        return cls(
            step_id=step_id,
            ok=bool(result.ok),
            status=result.status,
            summary=result.summary,
            modified_files=list(result.modified_files),
            validation=result.validation,
            needs_planner_resolution=bool(result.mismatch is not None or extras.get("planner_resolution_needed")),
            user_only_blocker=bool(extras.get("user_only_blocker")),
            extras=dict(extras),
        )


@dataclass(frozen=True)
class AggregatedDispatchResult:
    """Aggregated campaign result for one visible dispatch."""

    ok: bool
    summary: str
    status: str | None = None
    modified_files: list[str] = field(default_factory=list)
    validation: str | None = None
    step_results: list[StepResult] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_worker_result(self) -> WorkerDispatchResult:
        extras = dict(self.extras)
        if self.step_results:
            extras.setdefault("dispatch_step_results", [step.to_dict() for step in self.step_results])
        return WorkerDispatchResult(
            ok=self.ok,
            summary=self.summary,
            status=self.status,
            modified_files=list(self.modified_files),
            validation=self.validation,
            extras=extras,
        )


def plan_from_request(req: WorkerDispatchRequest) -> WorkerDispatchPlan:
    """Build a one-step compatibility plan from today's WorkerDispatchRequest."""
    step = WorkerStepSpec(
        id="step-1",
        title=req.summary or req.goal or "Worker dispatch",
        goal=req.goal,
        spec=req.spec,
        files=list(req.files),
        target_regions=[dict(region) for region in req.target_regions],
        acceptance=req.acceptance,
        validation_commands=list(req.validation_commands),
        required_outputs=list(req.required_outputs),
        non_goals=list(req.non_goals),
        expected_public_symbols=list(req.expected_public_symbols),
        expected_dataclass_fields={
            str(name): [str(field_name) for field_name in fields]
            for name, fields in req.expected_dataclass_fields.items()
        },
        forbidden_calls=list(req.forbidden_calls),
        forbidden_public_methods=list(req.forbidden_public_methods),
        risk_notes=list(req.risk_notes),
        validation_policy=StepValidationPolicy(
            tier="focused" if req.validation_commands else "structural",
            commands=list(req.validation_commands),
            required_proofs=[req.acceptance] if req.acceptance else [],
            reason="compatibility conversion from WorkerDispatchRequest",
        ),
    )
    return WorkerDispatchPlan(
        overall_goal=req.goal,
        visible_summary=req.summary,
        global_files=list(req.files),
        global_non_goals=list(req.non_goals),
        steps=[step],
    )


def request_for_step(
    plan: WorkerDispatchPlan,
    step: WorkerStepSpec,
    original: WorkerDispatchRequest,
) -> WorkerDispatchRequest:
    """Derive the bounded WorkerDispatchRequest for one active step."""
    non_goals = _dedupe([*plan.global_non_goals, *step.non_goals])
    validation_commands = list(step.validation_commands or step.validation_policy.commands)
    return WorkerDispatchRequest(
        goal=step.goal or plan.overall_goal or original.goal,
        files=list(step.files or plan.global_files or original.files),
        target_regions=[dict(region) for region in (step.target_regions or original.target_regions)],
        spec=step.spec or _fallback_step_spec(plan, step),
        acceptance=step.acceptance or original.acceptance,
        summary=step.title or plan.visible_summary or original.summary,
        run_command=original.run_command,
        allowed_responsibilities=list(original.allowed_responsibilities),
        forbidden_responsibilities=list(original.forbidden_responsibilities),
        required_outputs=list(step.required_outputs or original.required_outputs),
        validation_commands=validation_commands,
        risk_notes=list(step.risk_notes or original.risk_notes),
        non_goals=non_goals,
        expected_public_symbols=list(step.expected_public_symbols or original.expected_public_symbols),
        expected_dataclass_fields=(
            {name: list(fields) for name, fields in step.expected_dataclass_fields.items()}
            or {name: list(fields) for name, fields in original.expected_dataclass_fields.items()}
        ),
        forbidden_public_methods=list(step.forbidden_public_methods or original.forbidden_public_methods),
        forbidden_calls=list(step.forbidden_calls or original.forbidden_calls),
        contract=original.contract,
        task_shape=original.task_shape,
    )


def _fallback_step_spec(plan: WorkerDispatchPlan, step: WorkerStepSpec) -> str:
    parts = [
        f"Internal dispatch step {step.id}: {step.title}",
        "",
        f"Campaign goal: {plan.overall_goal}",
        f"Step goal: {step.goal}",
    ]
    if step.files:
        parts.append("Files: " + ", ".join(step.files))
    if step.non_goals or plan.global_non_goals:
        parts.append("Non-goals: " + "; ".join(_dedupe([*plan.global_non_goals, *step.non_goals])))
    return "\n".join(part for part in parts if part is not None)


def todo_tasks_from_plan(
    plan: WorkerDispatchPlan,
    *,
    active_step_id: str | None = None,
    completed_step_ids: set[str] | None = None,
    blocked_step_id: str | None = None,
) -> list[dict[str, Any]]:
    """Convert a WorkerDispatchPlan into canonical TODO tasks for the UI.

    Each task dict uses the stable fields the existing TodoListWidget expects:
    description, status (pending/active/done), plus step_id, id, and files.
    """
    completed: set[str] = completed_step_ids or set()
    tasks: list[dict[str, Any]] = []
    for step in plan.steps:
        task: dict[str, Any] = {
            "id": step.id,
            "step_id": step.id,
            "description": step.title or step.goal or "Worker step",
            "status": "pending",
        }
        if step.files:
            task["files"] = list(step.files)
        if step.acceptance:
            task["acceptance"] = step.acceptance[:200] if len(step.acceptance) > 200 else step.acceptance

        if step.id in completed:
            task["status"] = "done"
        elif step.id == active_step_id:
            task["status"] = "active"
            if step.id == blocked_step_id:
                task["blocked"] = True
        elif step.id == blocked_step_id:
            task["status"] = "active"
            task["blocked"] = True

        tasks.append(task)
    return tasks


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _dict_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _str_dict_list(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            result[str(key)] = [str(item) for item in value]
    return result


__all__ = [
    "AggregatedDispatchResult",
    "StepResult",
    "StepValidationPolicy",
    "WorkerDispatchPlan",
    "WorkerStepSpec",
    "plan_from_request",
    "request_for_step",
    "todo_tasks_from_plan",
]
