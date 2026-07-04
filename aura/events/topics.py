"""Event topic string constants for Aura's event bus.

Topics are grouped by subsystem. Each constant is a dotted string
matching the pattern ``<subsystem>.<event>``.
"""

# ── Work Artifact lifecycle ────────────────────────────────────────────────────
WORK_ARTIFACT_CREATED = "work_artifact.created"
WORK_ARTIFACT_UPDATED = "work_artifact.updated"
WORK_ARTIFACT_ITEM_READY = "work_artifact.item_ready"
WORK_ARTIFACT_ITEM_COMPLETED = "work_artifact.item_completed"

# ── Worker / tool execution ─────────────────────────────────────────────────
WORKER_TOOL_STARTED = "worker.tool_started"
WORKER_TOOL_FINISHED = "worker.tool_finished"
WORKER_FILE_CHANGED = "worker.file_changed"
WORKER_COMMAND_STARTED = "worker.command_started"
WORKER_COMMAND_FINISHED = "worker.command_finished"
WORKER_VALIDATION_STARTED = "worker.validation_started"
WORKER_VALIDATION_FINISHED = "worker.validation_finished"
WORKER_FINAL_REPORT_STARTED = "worker.final_report_started"
WORKER_FINAL_REPORT_FINISHED = "worker.final_report_finished"
WORKER_FAILED = "worker.failed"
WORKER_TODO_UPDATED = "worker.todo_updated"

# ── Lifecycle gate events ──────────────────────────────────────────────────
WORKER_PRE_TOOL_GATE_DECIDED = "worker.pre_tool_gate_decided"

# ── Wildcard — matches every event ──────────────────────────────────────────
ALL = "*"

# ── Convenience groupings for validation / introspection ────────────────────
WORK_ARTIFACT_TOPICS = frozenset({
    WORK_ARTIFACT_CREATED,
    WORK_ARTIFACT_UPDATED,
    WORK_ARTIFACT_ITEM_READY,
    WORK_ARTIFACT_ITEM_COMPLETED,
})

WORKER_TOPICS = frozenset({
    WORKER_TOOL_STARTED,
    WORKER_TOOL_FINISHED,
    WORKER_FILE_CHANGED,
    WORKER_COMMAND_STARTED,
    WORKER_COMMAND_FINISHED,
    WORKER_VALIDATION_STARTED,
    WORKER_VALIDATION_FINISHED,
    WORKER_FINAL_REPORT_STARTED,
    WORKER_FINAL_REPORT_FINISHED,
    WORKER_FAILED,
    WORKER_TODO_UPDATED,
    WORKER_PRE_TOOL_GATE_DECIDED,
})

ALL_TOPICS = WORK_ARTIFACT_TOPICS | WORKER_TOPICS
