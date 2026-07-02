"""Structured dispatch campaign and worker-step planning models.

A WorkerDispatchPlan is the durable internal campaign behind one visible
Planner -> Worker dispatch. Each WorkerStepSpec is intentionally bounded so
DispatchSession can hand the Worker one concrete work order at a time while the
user still sees one dispatch card, one Worker run, and one aggregate receipt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.dispatch_todo_manifest import (
    DispatchTodoItem,
    dispatch_todo_manifest_from_request,
    raw_dispatch_todo_checklist,
)
from aura.execution_checklist import build_execution_checklist_items


_BROAD_CAMPAIGN_PATTERNS = (
    r"\bmulti[- ]?(?:part|file|stage|step|phase)\b",
    r"\bsubsystem\b",
    r"\barchitect(?:ure|ural)?\b",
    r"\brefactor(?:ing)?\b",
    r"\bfeature\b",
    r"\bvalidation\b.*\b(?:rung|pipeline|orchestrat|stage|system|flow)\b",
    r"\b(?:build|create|implement|add)\b.*\b(?:system|subsystem|architecture|feature|workflow|pipeline|rung)\b",
)
_HIGH_RISK_PATTERNS = (
    r"\bauth(?:entication|orization)?\b",
    r"\bsecurity\b",
    r"\bcredential",
    r"\btoken\b",
    r"\bmigration\b",
    r"\bdatabase\b",
    r"\bconcurren",
    r"\bthread",
    r"\bprocess\b",
    r"\bdelete\b",
    r"\bdestructive\b",
)
_TINY_CLEANUP_PATTERNS = (
    r"\bcleanup\b",
    r"\bclean up\b",
    r"\btypo\b",
    r"\bcomment\b",
    r"\bdocstring\b",
    r"\bformat(?:ting)?\b",
    r"\blint\b",
    r"\brename\b",
)
_GENERIC_STEP_TITLES = {
    "worker step",
    "implementation",
    "implement",
    "make changes",
    "update files",
    "do the work",
    "complete task",
    "fix",
    "change",
}


@dataclass(frozen=True)
class CampaignValidationResult:
    """Planner-visible validation for WorkerDispatchPlan campaign shape."""

    ok: bool
    requires_steps: bool = False
    errors: list[str] = field(default_factory=list)
    checklist_errors: list[str] = field(default_factory=list)


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
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "commands": list(self.commands),
            "required_proofs": list(self.required_proofs),
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
    checklist_item_ids: list[str] = field(default_factory=list)

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
            "checklist_item_ids": list(self.checklist_item_ids),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkerStepSpec":
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            goal=str(raw.get("goal") or ""),
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
            checklist_item_ids=_str_list(raw.get("checklist_item_ids")),
        )


@dataclass(frozen=True)
class WorkerDispatchPlan:
    """Internal campaign plan behind a single visible Worker dispatch."""

    overall_goal: str
    visible_summary: str = ""
    global_files: list[str] = field(default_factory=list)
    global_non_goals: list[str] = field(default_factory=list)
    steps: list[WorkerStepSpec] = field(default_factory=list)
    visible_checklist: list[DispatchTodoItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_goal": self.overall_goal,
            "visible_summary": self.visible_summary,
            "global_files": list(self.global_files),
            "global_non_goals": list(self.global_non_goals),
            "steps": [step.to_dict() for step in self.steps],
            "visible_checklist": [item.to_dict() for item in self.visible_checklist],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkerDispatchPlan":
        if not isinstance(raw, dict):
            raw = {}
        steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
        checklist = raw_dispatch_todo_checklist(raw)
        return cls(
            overall_goal=str(raw.get("overall_goal") or raw.get("goal") or ""),
            visible_summary=str(raw.get("visible_summary") or raw.get("summary") or ""),
            global_files=_str_list(raw.get("global_files")),
            global_non_goals=_str_list(raw.get("global_non_goals")),
            steps=[WorkerStepSpec.from_dict(step) for step in steps],
            visible_checklist=[DispatchTodoItem.from_dict(item) for item in checklist],
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
    has_mismatch: bool = False
    user_only_blocker: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step_id": self.step_id,
            "ok": self.ok,
            "summary": self.summary,
            "modified_files": list(self.modified_files),
            "has_mismatch": self.has_mismatch,
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
            has_mismatch=bool(result.mismatch is not None or extras.get("mismatch_detected")),
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
    if req.steps:
        checklist = dispatch_todo_manifest_from_request(req)
        return WorkerDispatchPlan(
            overall_goal=req.goal,
            visible_summary=req.summary,
            global_files=list(req.files),
            global_non_goals=list(req.non_goals),
            steps=list(req.steps),
            visible_checklist=checklist,
        )

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
        visible_checklist=dispatch_todo_manifest_from_request(replace(req, steps=[step])),
    )


def request_for_step(
    plan: WorkerDispatchPlan,
    step: WorkerStepSpec,
    original: WorkerDispatchRequest,
) -> WorkerDispatchRequest:
    """Derive the bounded WorkerDispatchRequest for one active step."""
    explicit_campaign = bool(original.steps)
    if explicit_campaign:
        files = _step_files(step)
        target_regions = [dict(region) for region in step.target_regions]
        goal = step.goal
        spec = step.spec or _fallback_step_spec(plan, step, include_campaign=False)
        acceptance = step.acceptance
        summary = step.title or step.goal
        required_outputs = list(step.required_outputs)
        validation_commands = list(step.validation_commands or step.validation_policy.commands)
        risk_notes = _dedupe([*original.risk_notes, *step.risk_notes])
        expected_public_symbols = list(step.expected_public_symbols)
        expected_dataclass_fields = {
            name: list(fields) for name, fields in step.expected_dataclass_fields.items()
        }
        forbidden_public_methods = _dedupe([
            *original.forbidden_public_methods,
            *step.forbidden_public_methods,
        ])
        forbidden_calls = _dedupe([*original.forbidden_calls, *step.forbidden_calls])
    else:
        files = list(step.files or plan.global_files or original.files)
        target_regions = [dict(region) for region in (step.target_regions or original.target_regions)]
        goal = step.goal or plan.overall_goal or original.goal
        spec = step.spec or _fallback_step_spec(plan, step, include_campaign=True)
        acceptance = step.acceptance or original.acceptance
        summary = step.title or plan.visible_summary or original.summary
        required_outputs = list(step.required_outputs or original.required_outputs)
        validation_commands = list(step.validation_commands or step.validation_policy.commands)
        risk_notes = list(step.risk_notes or original.risk_notes)
        expected_public_symbols = list(step.expected_public_symbols or original.expected_public_symbols)
        expected_dataclass_fields = (
            {name: list(fields) for name, fields in step.expected_dataclass_fields.items()}
            or {name: list(fields) for name, fields in original.expected_dataclass_fields.items()}
        )
        forbidden_public_methods = list(step.forbidden_public_methods or original.forbidden_public_methods)
        forbidden_calls = list(step.forbidden_calls or original.forbidden_calls)

    non_goals = _dedupe([*plan.global_non_goals, *step.non_goals])
    return WorkerDispatchRequest(
        goal=goal,
        files=files,
        target_regions=target_regions,
        spec=spec,
        acceptance=acceptance,
        summary=summary,
        run_command=original.run_command,
        allowed_responsibilities=list(original.allowed_responsibilities),
        forbidden_responsibilities=list(original.forbidden_responsibilities),
        required_outputs=required_outputs,
        validation_commands=validation_commands,
        risk_notes=risk_notes,
        non_goals=non_goals,
        expected_public_symbols=expected_public_symbols,
        expected_dataclass_fields=expected_dataclass_fields,
        forbidden_public_methods=forbidden_public_methods,
        forbidden_calls=forbidden_calls,
        contract=original.contract,
        task_shape=original.task_shape,
    )


def _fallback_step_spec(
    plan: WorkerDispatchPlan,
    step: WorkerStepSpec,
    *,
    include_campaign: bool,
) -> str:
    parts = [
        f"Internal dispatch step {step.id}: {step.title}",
        "",
        f"Step goal: {step.goal}",
    ]
    if include_campaign:
        parts.insert(2, f"Campaign goal: {plan.overall_goal}")
    step_files = _step_files(step)
    if step_files:
        parts.append("Files: " + ", ".join(step_files))
    if step.non_goals or plan.global_non_goals:
        parts.append("Non-goals: " + "; ".join(_dedupe([*plan.global_non_goals, *step.non_goals])))
    return "\n".join(part for part in parts if part is not None)


def validate_dispatch_campaign(req: WorkerDispatchRequest) -> CampaignValidationResult:
    """Validate that broad implementation work is decomposed into Worker steps.

    Flat dispatch fields remain a compatibility path for tiny work. Anything
    that looks multi-file, high-risk, subsystem/architecture/feature oriented,
    refactor-like, or multi-stage validation must arrive as a real campaign.
    """
    requires_steps = _request_requires_campaign(req)
    errors: list[str] = []

    if requires_steps and not req.steps:
        errors.append(
            "Broad implementation dispatches must include a decomposed steps campaign."
        )

    if req.steps:
        errors.extend(_step_boundary_errors(req, requires_steps=requires_steps))

    checklist_errors = _checklist_errors(req, requires_steps=requires_steps)

    return CampaignValidationResult(
        ok=not errors and not checklist_errors,
        requires_steps=requires_steps,
        errors=errors,
        checklist_errors=checklist_errors,
    )


def _request_requires_campaign(req: WorkerDispatchRequest) -> bool:
    if len(req.files) > 1:
        return True
    text = _request_text(req)
    if _matches_any(text, _BROAD_CAMPAIGN_PATTERNS):
        return not _looks_bounded_single_file_repair(req)
    if req.risk_notes or _matches_any(text, _HIGH_RISK_PATTERNS):
        return True
    if len(req.validation_commands) > 1:
        return True
    if _looks_multi_stage_validation(req.acceptance):
        return True
    task_shape = req.task_shape
    if (
        task_shape is not None
        and task_shape.task_kind in {"new_tool_or_app", "refactor"}
        and not _looks_tiny_cleanup(req)
    ):
        return True
    return False


def _step_boundary_errors(
    req: WorkerDispatchRequest,
    *,
    requires_steps: bool,
) -> list[str]:
    errors: list[str] = []
    steps = list(req.steps)
    if not steps:
        return errors

    if requires_steps and len(steps) == 1:
        step = steps[0]
        step_files = _step_files(step) or _dedupe(req.files)
        if len(step_files) >= 3:
            errors.append("A single campaign step cannot cover 3 or more files.")
        if len(_dedupe(req.files)) > 1 and len(step_files) >= len(_dedupe(req.files)):
            errors.append("Broad multi-file dispatches must split file work across multiple bounded steps.")
        if _boundary_text_is_top_level(step.title, req):
            errors.append("step 1 title repeats the top-level dispatch instead of naming a step boundary.")
        if _boundary_text_is_top_level(step.goal, req):
            errors.append("step 1 goal repeats the top-level dispatch instead of naming a step boundary.")
        if _boundary_text_is_top_level(step.spec, req):
            errors.append("step 1 spec must be bounded to that step, not the full campaign.")
        if _boundary_text_is_top_level(step.acceptance, req):
            errors.append("step 1 acceptance repeats the top-level dispatch instead of a step-specific pass/fail check.")

    for index, step in enumerate(steps, start=1):
        prefix = f"step {index}"
        needs_distinct_boundary = len(steps) > 1
        if not step.id.strip():
            errors.append(f"{prefix} is missing id.")
        if not _useful_step_title(
            step.title,
            req,
            needs_distinct_boundary=needs_distinct_boundary,
        ):
            errors.append(f"{prefix} needs a short, specific title.")
        if not step.goal.strip():
            errors.append(f"{prefix} is missing goal.")
        if not step.spec.strip():
            errors.append(f"{prefix} is missing a bounded spec.")
        elif needs_distinct_boundary and _boundary_text_is_top_level(step.spec, req):
            errors.append(f"{prefix} spec must be bounded to that step, not the full campaign.")
        if needs_distinct_boundary and _boundary_text_is_top_level(step.goal, req):
            errors.append(f"{prefix} goal repeats the top-level dispatch instead of naming a step boundary.")
        if not _step_files(step):
            errors.append(f"{prefix} is missing files; each campaign step must name its own file scope.")
        if not step.acceptance.strip():
            errors.append(f"{prefix} is missing acceptance.")
        elif needs_distinct_boundary and _boundary_text_is_top_level(step.acceptance, req):
            errors.append(f"{prefix} acceptance repeats the top-level dispatch instead of a step-specific pass/fail check.")

    normalized_titles = [_normalize_boundary_text(step.title) for step in steps]
    useful_titles = [title for title in normalized_titles if title]
    if len(steps) > 1 and len(set(useful_titles)) < len(useful_titles):
        errors.append("Campaign steps need distinct titles.")

    return _dedupe(errors)


def _checklist_errors(
    req: WorkerDispatchRequest,
    *,
    requires_steps: bool,
) -> list[str]:
    if not requires_steps:
        return []

    rows = build_execution_checklist_items(req)
    if not rows:
        return [
            "Broad implementation dispatches must produce a concrete visible execution checklist."
        ]

    if req.steps and len(rows) < len(req.steps):
        return [
            "Broad implementation dispatches must produce checklist rows that cover every step."
        ]

    if not req.steps and len(rows) == 1:
        return [
            "Broad implementation dispatches must not collapse to a single campaign-title checklist row."
        ]

    return []


def _request_text(req: WorkerDispatchRequest) -> str:
    parts = [
        req.goal,
        req.summary,
        req.spec,
        req.acceptance,
        " ".join(req.files),
        " ".join(req.required_outputs),
        " ".join(req.risk_notes),
    ]
    return " ".join(str(part or "") for part in parts)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _looks_multi_stage_validation(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered:
        return False
    markers = (
        "multi-stage validation",
        "multi stage validation",
        "validation pipeline",
        "validation rung",
        "compileall",
    )
    if sum(1 for marker in markers if marker in lowered) >= 1 and (
        " and " in lowered or ";" in lowered or "\n" in lowered
    ):
        return True
    command_markers = ("pytest", "compileall", "py_compile", "npm test", "ruff", "mypy")
    return sum(1 for marker in command_markers if marker in lowered) > 1


def _useful_step_title(
    title: str,
    req: WorkerDispatchRequest,
    *,
    needs_distinct_boundary: bool,
) -> bool:
    normalized = _normalize_boundary_text(title)
    if not normalized:
        return False
    if normalized in _GENERIC_STEP_TITLES:
        return False
    if needs_distinct_boundary and _boundary_text_is_top_level(title, req):
        return False
    return True


def _boundary_text_is_top_level(value: str, req: WorkerDispatchRequest) -> bool:
    normalized = _normalize_boundary_text(value)
    if not normalized:
        return False
    candidates = [
        req.goal,
        req.summary,
        req.spec,
        req.acceptance,
        _first_sentence(req.spec),
    ]
    return normalized in {
        _normalize_boundary_text(candidate)
        for candidate in candidates
        if _normalize_boundary_text(candidate)
    }


def _first_sentence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return ""
    first_line = next((line.strip() for line in stripped.splitlines() if line.strip()), "")
    match = re.split(r"(?<=[.!?])\s+", first_line, maxsplit=1)
    return match[0] if match else first_line


def _normalize_boundary_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return text.strip(" .:-")


def _looks_tiny_cleanup(req: WorkerDispatchRequest) -> bool:
    if len(req.files) != 1:
        return False
    if req.risk_notes or len(req.validation_commands) > 1:
        return False
    text = _request_text(req)
    return _matches_any(text, _TINY_CLEANUP_PATTERNS) and not (
        _matches_any(text, _BROAD_CAMPAIGN_PATTERNS)
        or _matches_any(text, _HIGH_RISK_PATTERNS)
    )


def _looks_bounded_single_file_repair(req: WorkerDispatchRequest) -> bool:
    if len(req.files) != 1:
        return False
    if req.risk_notes or len(req.validation_commands) > 1:
        return False
    text = _request_text(req)
    if _matches_any(text, _HIGH_RISK_PATTERNS):
        return False
    if _looks_multi_stage_validation(req.acceptance):
        return False
    if not req.goal.strip() or not req.spec.strip() or not req.acceptance.strip():
        return False
    return _matches_any(text, (r"\brepair\b", r"\bfix\b", r"\brefactor(?:ing)?\b"))


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


def _files_from_target_regions(target_regions: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for region in target_regions:
        if not isinstance(region, dict):
            continue
        path = str(region.get("path") or "").strip()
        if path:
            paths.append(path)
    return _dedupe(paths)


def _step_files(step: WorkerStepSpec) -> list[str]:
    return _dedupe(list(step.files)) or _files_from_target_regions(step.target_regions)


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
    "CampaignValidationResult",
    "StepResult",
    "StepValidationPolicy",
    "WorkerDispatchPlan",
    "WorkerStepSpec",
    "plan_from_request",
    "request_for_step",
    "validate_dispatch_campaign",
]
