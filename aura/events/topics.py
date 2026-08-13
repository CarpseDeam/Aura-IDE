"""Event topic string constants for Aura's event bus.

Topics are grouped by subsystem. Each constant is a dotted string
matching the pattern ``<subsystem>.<event>``.
"""

# ── Execution / tool execution ─────────────────────────────────────────────────
EXECUTION_TOOL_STARTED = "execution.tool_started"
EXECUTION_TOOL_FINISHED = "execution.tool_finished"
EXECUTION_FILE_CHANGED = "execution.file_changed"
EXECUTION_COMMAND_STARTED = "execution.command_started"
EXECUTION_COMMAND_FINISHED = "execution.command_finished"
EXECUTION_VALIDATION_STARTED = "execution.validation_started"
EXECUTION_VALIDATION_FINISHED = "execution.validation_finished"
EXECUTION_FINAL_REPORT_STARTED = "execution.final_report_started"
EXECUTION_FINAL_REPORT_FINISHED = "execution.final_report_finished"
EXECUTION_FAILED = "execution.failed"
TASK_CHECKLIST_UPDATED = "task_checklist.updated"

# ── Lifecycle gate events ──────────────────────────────────────────────────
EXECUTION_PRE_TOOL_GATE_DECIDED = "execution.pre_tool_gate_decided"

# ── Wildcard — matches every event ──────────────────────────────────────────
ALL = "*"

# ── Convenience groupings for validation / introspection ────────────────────
EXECUTION_TOPICS = frozenset({
    EXECUTION_TOOL_STARTED,
    EXECUTION_TOOL_FINISHED,
    EXECUTION_FILE_CHANGED,
    EXECUTION_COMMAND_STARTED,
    EXECUTION_COMMAND_FINISHED,
    EXECUTION_VALIDATION_STARTED,
    EXECUTION_VALIDATION_FINISHED,
    EXECUTION_FINAL_REPORT_STARTED,
    EXECUTION_FINAL_REPORT_FINISHED,
    EXECUTION_FAILED,
    TASK_CHECKLIST_UPDATED,
    EXECUTION_PRE_TOOL_GATE_DECIDED,
})

ALL_TOPICS = EXECUTION_TOPICS
