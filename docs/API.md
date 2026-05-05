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
