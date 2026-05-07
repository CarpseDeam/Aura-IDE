from aura.conversation.history import History

# Scenario: massive tool result in an OLD (non-preserved) middle turn.
h = History()
h.set_system("you are a helpful assistant")

# Turn 0 (first — always preserved)
h.append_user_text("Help me analyze the codebase")
h.append_assistant({"role": "assistant", "content": "Sure, let me get started."})

# Turn 1 (OLD middle — eligible for tool truncation)
h.append_user_text("read the main file")
h.append_tool_result("call_read", "X" * 300000)  # 300K chars — over budget!
h.append_assistant({"role": "assistant", "content": "Here's what I found."})

# Turns 2-8 (more middle turns)
for i in range(2, 9):
    h.append_user_text(f"step {i}")
    h.append_tool_result(f"call_{i}", "Y" * 100)
    h.append_assistant({"role": "assistant", "content": f"result {i}"})

# Turn 9 (last — preserved)
h.append_user_text("thanks")
h.append_assistant({"role": "assistant", "content": "you're welcome"})

est_before = h.estimate_tokens()
api = h.for_api()
est_after = h.estimate_tokens()

print(f"Estimate before: {est_before}")
print(f"Estimate after:  {est_after}")
print(f"Reduction:       {est_before - est_after} tokens")
print(f"Under 60K limit: {est_after <= 60000}")
print(f"API msgs count:  {len(api)}")
print()

# Show all tool messages in the API output
for m in api:
    if m.get("role") == "tool":
        c = m.get("content", "")
        cid = m.get("tool_call_id", "?")
        truncated = "[... result truncated from" in c
        print(f"  Tool {cid}: {len(c)} chars {'(TRUNCATED)' if truncated else '(intact)'}")
