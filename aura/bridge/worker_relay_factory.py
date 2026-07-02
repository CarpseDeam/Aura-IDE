"""WorkerEventRelay factory — constructs and wires relay signals.

Owns WorkerEventRelay creation and signal wiring only.
Does NOT own Worker execution, completion classification, validation selector
policy, or pending state.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt

from aura.bridge.event_relay import WorkerEventRelay
from aura.events import EventBus


def create_worker_relay(
    *,
    approval_proxy: Any,
    worker_model: str,
    dispatch_proxy: Any,
    event_bus: EventBus,
    suppress_final_report_activity: bool = False,
) -> WorkerEventRelay:
    """Construct a WorkerEventRelay and wire every signal to *dispatch_proxy*.

    The caller (_DispatchProxy) owns the Qt signal declarations and passes
    itself as *dispatch_proxy* so the factory can connect each relay signal
    to the matching proxy signal.  The factory does not become a second
    dispatch proxy — it only builds and wires the relay.

    suppress_final_report_activity: When True, the relay will not emit
        WORKER_FINAL_REPORT_STARTED / WORKER_FINAL_REPORT_FINISHED events
        on the event bus. Used for internal DispatchSession worker steps
        so the UI does not see false "Final report started/completed"
        Activity entries between steps.
    """
    relay = WorkerEventRelay(
        approval_proxy=approval_proxy,
        worker_model=worker_model,
        suppress_final_report_activity=suppress_final_report_activity,
        event_bus=event_bus,
    )
    # Stream events
    relay.reasoningDelta.connect(dispatch_proxy.workerReasoningDelta)
    relay.contentDelta.connect(dispatch_proxy.workerContentDelta)
    # Tool-call lifecycle
    relay.toolCallStart.connect(dispatch_proxy.workerToolCallStart)
    relay.toolCallArgs.connect(dispatch_proxy.workerToolCallArgs)
    relay.toolCallEnd.connect(dispatch_proxy.workerToolCallEnd)
    # Usage / completion
    relay.usage.connect(dispatch_proxy.workerUsage)
    relay.streamDone.connect(dispatch_proxy.workerStreamDone)
    relay.apiError.connect(dispatch_proxy.workerApiError)
    # Tool results
    relay.toolResult.connect(dispatch_proxy.workerToolResult)
    relay.diffDecided.connect(dispatch_proxy.workerDiffDecided)
    # Terminal / agent process
    relay.terminalOutput.connect(dispatch_proxy.workerTerminalOutput)
    relay.agentProcessStarted.connect(dispatch_proxy.workerAgentProcessStarted)
    relay.agentProcessOutput.connect(dispatch_proxy.workerAgentProcessOutput)
    relay.agentProcessFinished.connect(dispatch_proxy.workerAgentProcessFinished)

    # ---- WorkflowState (DirectConnection on planner thread) ----
    # These run synchronously on the planner thread so they can update
    # _active_workflow while request_dispatch / session.run() is on the
    # call stack.  The regular (Auto) connections above handle GUI update.
    relay.toolCallStart.connect(
        dispatch_proxy._workflow_tool_started, Qt.DirectConnection
    )
    relay.toolResult.connect(
        dispatch_proxy._workflow_tool_result, Qt.DirectConnection
    )

    return relay


__all__ = [
    "create_worker_relay",
]
