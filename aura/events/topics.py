"""Event topic string constants for Aura's event bus.

Topics are grouped by subsystem. Each constant is a dotted string
matching the pattern ``<subsystem>.<event>``.
"""

# ── Execution / tool execution ─────────────────────────────────────────────────
EXECUTION_COMMAND_STARTED = "execution.command_started"
EXECUTION_COMMAND_FINISHED = "execution.command_finished"
EXECUTION_VALIDATION_FINISHED = "execution.validation_finished"
TASK_CHECKLIST_UPDATED = "task_checklist.updated"

# ── Lifecycle gate events ──────────────────────────────────────────────────
EXECUTION_PRE_TOOL_GATE_DECIDED = "execution.pre_tool_gate_decided"

# ── Wildcard — matches every event ──────────────────────────────────────────
ALL = "*"

# ── Convenience groupings for validation / introspection ────────────────────
EXECUTION_TOPICS = frozenset({
    EXECUTION_COMMAND_STARTED,
    EXECUTION_COMMAND_FINISHED,
    EXECUTION_VALIDATION_FINISHED,
    TASK_CHECKLIST_UPDATED,
    EXECUTION_PRE_TOOL_GATE_DECIDED,
})

ALL_TOPICS = EXECUTION_TOPICS
