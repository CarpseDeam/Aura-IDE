"""Conversation history and the tool-loop manager."""

from aura.conversation.critic_dispatch import CriticCallback, CriticRequest
from aura.conversation.critic_verdict import CriticFinding, CriticVerdict
from aura.conversation.dispatch import (
    DispatchCallback,
    ExplicitSpecContract,
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerMismatch,
    WorkerTaskSpec,
    infer_outcome_status,
    normalize_worker_task,
)
from aura.conversation.dispatch_plan import (
    AggregatedDispatchResult,
    StepResult,
    StepValidationPolicy,
    WorkerDispatchPlan,
    WorkerStepSpec,
    plan_from_request,
    request_for_step,
)
from aura.conversation.dispatch_todo_manifest import (
    DispatchTodoItem,
    dispatch_todo_manifest_from_request,
    ensure_dispatch_todo_checklist,
    todo_tasks_from_plan,
)
from aura.conversation.history import History
from aura.conversation.task_shape import TaskShape, infer_task_shape
from aura.conversation.worker_outcome import (
    WorkerOutcomeStatus,
    normalize_outcome_status,
)
from aura.conversation.workflow_state import (
    ValidationCommandRun,
    ValidationStatus,
    WorkflowState,
    WorkflowStatus,
)


def __getattr__(name: str):
    if name == "ConversationManager":
        from aura.conversation.manager import ConversationManager

        return ConversationManager
    raise AttributeError(name)


__all__ = [
    "History",
    "ConversationManager",
    "ExplicitSpecContract",
    "WorkerDispatchRequest",
    "WorkerDispatchResult",
    "WorkerMismatch",
    "WorkerOutcomeStatus",
    "WorkerTaskSpec",
    "WorkerDispatchPlan",
    "WorkerStepSpec",
    "StepResult",
    "StepValidationPolicy",
    "AggregatedDispatchResult",
    "DispatchTodoItem",
    "TaskShape",
    "DispatchCallback",
    "CriticCallback",
    "CriticFinding",
    "CriticRequest",
    "CriticVerdict",
    "infer_task_shape",
    "infer_outcome_status",
    "normalize_outcome_status",
    "normalize_worker_task",
    "dispatch_todo_manifest_from_request",
    "ensure_dispatch_todo_checklist",
    "plan_from_request",
    "request_for_step",
    "todo_tasks_from_plan",
    "ValidationCommandRun",
    "ValidationStatus",
    "WorkflowState",
    "WorkflowStatus",
]
