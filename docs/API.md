# API Reference

## Bridge

### ConversationBridge

Signals:
- `started()`: Fired when the conversation starts.
- `finished()`: Fired when the conversation finishes.
- `usageEmitted(prompt: int, completion: int, hit: int, miss: int)`: Fired with token usage.
- `usageWithModel(model_id: str, prompt: int, completion: int, hit: int, miss: int)`: Fired with model ID and token usage.
- `apiError(status: int, message: str)`: Fired on API errors.
- `streamDone(finish_reason: str, full_message: dict)`: Fired when streaming is complete.

#### Planner / Worker Signals
- `workerDispatchRequested(tool_id, goal, files, spec, acceptance)`: Fired when the planner requests a worker dispatch.
- `workerStarted(tool_id)`: Fired when a worker starts executing.
- `workerFinished(tool_id, ok, summary)`: Fired when a worker completes.
- `workerReasoningDelta(tool_id, text)`: Streaming reasoning from the worker.
- `workerContentDelta(tool_id, text)`: Streaming content from the worker.
- `workerToolCallStart(parent_id, worker_id, name)`: Worker tool call start.
- `workerToolResult(parent_id, worker_id, name, ok, result, extras)`: Worker tool call result.
- `workerDiffDecided(parent_id, worker_id, decision, path, old, new, is_new)`: Fired when a worker's write is approved/rejected.
- `workerUsage(tool_id, model, prompt, comp, hit, miss)`: Worker-specific token usage.

### Manager

#### ConversationManager

Methods:
- `send(on_event, approval_cb, cancel_event, model, thinking, dispatch_cb)`: Main entry point for the model loop. `dispatch_cb` is required for Planner role.
