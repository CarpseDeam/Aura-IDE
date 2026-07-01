"""WorkerEventRelay factory — constructs and wires relay signals.

Owns WorkerEventRelay creation and signal wiring only.
Does NOT own Worker execution, completion classification, validation selector
policy, or pending state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aura.bridge.event_relay import WorkerEventRelay


def create_worker_relay(
    *,
    approval_proxy: Any,
    worker_model: str,
    dispatch_proxy: Any,
    todo_relay_callback: Callable[[str, list], None],
) -> WorkerEventRelay:
    """Construct a WorkerEventRelay and wire every signal to *dispatch_proxy*.

    The caller (_DispatchProxy) owns the Qt signal declarations and passes
    itself as *dispatch_proxy* so the factory can connect each relay signal
    to the matching proxy signal.  The factory does not become a second
    dispatch proxy — it only builds and wires the relay.
    """
    relay = WorkerEventRelay(
        approval_proxy=approval_proxy,
        worker_model=worker_model,
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
    # TODO — routed through the caller's relay callback so canonical dispatch
    # can suppress Worker-local updates.
    relay.todoListUpdated.connect(todo_relay_callback)
    # Terminal / agent process
    relay.terminalOutput.connect(dispatch_proxy.workerTerminalOutput)
    relay.agentProcessStarted.connect(dispatch_proxy.workerAgentProcessStarted)
    relay.agentProcessOutput.connect(dispatch_proxy.workerAgentProcessOutput)
    relay.agentProcessFinished.connect(dispatch_proxy.workerAgentProcessFinished)
    return relay


__all__ = [
    "create_worker_relay",
]
