"""Conversation history and the tool-loop manager."""

from aura.conversation.dispatch import (
    DispatchCallback,
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerMismatch,
    WorkerOutcomeStatus,
    WorkerTaskSpec,
    infer_outcome_status,
    normalize_outcome_status,
    normalize_worker_task,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.task_shape import TaskShape, infer_task_shape
from aura.conversation.workflow_state import (
    ValidationCommandRun,
    ValidationStatus,
    WorkflowState,
    WorkflowStatus,
)

__all__ = [
    "History",
    "ConversationManager",
    "WorkerDispatchRequest",
    "WorkerDispatchResult",
    "WorkerMismatch",
    "WorkerOutcomeStatus",
    "WorkerTaskSpec",
    "TaskShape",
    "DispatchCallback",
    "infer_task_shape",
    "infer_outcome_status",
    "normalize_outcome_status",
    "normalize_worker_task",
    "ValidationCommandRun",
    "ValidationStatus",
    "WorkflowState",
    "WorkflowStatus",
]
