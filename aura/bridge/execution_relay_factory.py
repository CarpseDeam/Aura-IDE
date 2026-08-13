"""ExecutionEventRelay factory — constructs and wires relay signals.

Owns ExecutionEventRelay creation and signal wiring only.
Does NOT own execution, completion classification, validation selector
policy, or pending state.
"""

from __future__ import annotations

from typing import Any

from aura.bridge.execution_event_relay import ExecutionEventRelay
from aura.events import EventBus


def create_execution_relay(
    *,
    approval_proxy: Any,
    execution_model: str,
    projection_target: Any,
    event_bus: EventBus,
    suppress_content_projection: bool = False,
) -> ExecutionEventRelay:
    """Construct an ExecutionEventRelay and wire every signal to the projection target.

    suppress_content_projection: When True, assistant prose is not forwarded
        into the workspace ``executionContentDelta`` stream.  Conversation prose
        is chat-owned; a caller that already routes it to ChatView must not
        also duplicate it into the ephemeral Execution Log.
    """
    relay = ExecutionEventRelay(
        approval_proxy=approval_proxy,
        execution_model=execution_model,
        suppress_final_report_activity=False,
        event_bus=event_bus,
    )
    # Stream events
    relay.reasoningDelta.connect(projection_target.executionReasoningDelta)
    if not suppress_content_projection:
        relay.contentDelta.connect(projection_target.executionContentDelta)
    # Tool-call lifecycle
    relay.toolCallStart.connect(projection_target.executionToolCallStart)
    relay.toolCallArgs.connect(projection_target.executionToolCallArgs)
    relay.toolCallEnd.connect(projection_target.executionToolCallEnd)
    # Usage / completion
    relay.usage.connect(projection_target.executionUsage)
    relay.streamDone.connect(projection_target.executionStreamDone)
    relay.apiError.connect(projection_target.executionApiError)
    # Tool results
    relay.toolResult.connect(projection_target.executionToolResult)
    relay.diffDecided.connect(projection_target.executionDiffDecided)
    # Terminal / agent process
    relay.terminalOutput.connect(projection_target.executionTerminalOutput)
    relay.agentProcessStarted.connect(projection_target.executionAgentProcessStarted)
    relay.agentProcessOutput.connect(projection_target.executionAgentProcessOutput)
    relay.agentProcessFinished.connect(projection_target.executionAgentProcessFinished)

    return relay


__all__ = [
    "create_execution_relay",
]
