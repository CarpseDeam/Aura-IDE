"""Event topic string constants for Aura's event bus.

Topics are grouped by subsystem. Each constant is a dotted string
matching the pattern ``<subsystem>.<event>``.
"""

# ── Dispatch lifecycle ──────────────────────────────────────────────────────
DISPATCH_CHECKLIST_DECLARED = "dispatch.checklist_declared"
DISPATCH_CAMPAIGN_STARTED = "dispatch.campaign_started"
DISPATCH_STEP_STARTED = "dispatch.step_started"
DISPATCH_STEP_COMPLETED = "dispatch.step_completed"
DISPATCH_CAMPAIGN_FINISHED = "dispatch.campaign_finished"

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

# ── Wildcard — matches every event ──────────────────────────────────────────
ALL = "*"

# ── Convenience groupings for validation / introspection ────────────────────
DISPATCH_TOPICS = frozenset({
    DISPATCH_CHECKLIST_DECLARED,
    DISPATCH_CAMPAIGN_STARTED,
    DISPATCH_STEP_STARTED,
    DISPATCH_STEP_COMPLETED,
    DISPATCH_CAMPAIGN_FINISHED,
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
})

ALL_TOPICS = DISPATCH_TOPICS | WORKER_TOPICS
