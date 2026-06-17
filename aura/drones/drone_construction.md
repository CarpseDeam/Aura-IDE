# Drone Construction Spec

You are building a **Drone** — a single-verb, reusable worker that lives as a folder on disk. A Drone does one job, reads JSON from stdin, writes JSON to stdout, and leaves a receipt that proves what it did.

## Folder structure

Every Drone is a folder containing at minimum:

```
<drone-id>/
  drone.json      ← manifest (required)
  main.py         ← entrypoint (required)
```

Additional files (helpers, templates, data) are allowed when the job needs them. Keep it flat — no nested packages unless genuinely warranted.

## drone.json — the manifest

Every manifest MUST include these fields:

```json
{
  "id": "<slug>",
  "name": "<Human Name>",
  "description": "<One sentence: what it does>",
  "instructions": "<Operational prose: what the Drone does, what it expects in the goal string, what it returns. This is what the runner reads to understand how to invoke it.>",
  "write_policy": "read_only | ask_before_writes | normal_diff_approval",
  "runtime": "python",
  "entrypoint": {
    "kind": "command",
    "command": ["python", "main.py"],
    "protocol": "json-stdio"
  },
  "budget": {
    "timeout_seconds": 120
  },
  "manifest_version": "1",
  "scope": "global",
  "input_contract": { ... },
  "cargo_contract": { ... },
  "output_contract": { ... }
}
```

### Runtime payload vs. contracts

Two different things flow into a Drone. Don't weld them.

**Runtime payload (always injected, never declared).** The runner hands every Drone a stdin object with `goal`, `workspace_root`, `drone_id`, and `upstream`. Read them with `.get()`. They are guaranteed plumbing — never list them in any contract.

**Cargo (the chain data).** `upstream` holds the `cargo` emitted by the Drones feeding this one, keyed by node id. `input_contract` is the cargo you need from upstream; `cargo_contract` is the cargo you emit. These two — and only these — are what the chain validator type-checks against each other.

### Field rules
- **id**: lowercase slug, hyphens only. Must match the folder name.
- **name**: human-readable, title case.
- **description**: one sentence. What it does, not how.
- **instructions**: operational detail. What the goal string should contain, what formats it accepts, what the output shape means. The runner and the planner both read this to know how to invoke the Drone.
- **write_policy**: `read_only` for pure reads/analysis. `normal_diff_approval` for anything that modifies files, pushes, or has side effects. `ask_before_writes` for sensitive writes that need per-action confirmation.
- **input_contract**: JSON Schema fragment describing ONLY the cargo this Drone consumes from upstream — the fields it expects to find in `upstream`. Same vocabulary as a producer's `cargo_contract`, viewed from the receiving end. Do NOT include runtime fields (`goal`, `workspace_root`, `drone_id`, `upstream`) — the runner always injects those; they are never part of a contract. A chain-head Drone that consumes no upstream cargo leaves this `{}`.
- **cargo_contract**: JSON Schema fragment describing the structured output the Drone produces. This is what downstream Drones in a chain consume. Be precise — typed fields, required arrays, enums where values are known.
- **output_contract**: JSON Schema fragment describing the full stdout output shape, including ok/error envelopes. Use `oneOf` for success vs failure shapes.

### Contract quality

Contracts MUST be **typed JSON Schema**, not prose strings. Bad:

```json
"output_contract": "Return JSON with ok and a message"
```

Good:

```json
"output_contract": {
  "oneOf": [
    {
      "type": "object",
      "properties": {
        "ok": {"const": true},
        "files_scanned": {"type": "integer"},
        "results": {"type": "array", "items": {"type": "object"}},
        "summary": {"type": "string"}
      },
      "required": ["ok", "files_scanned", "results", "summary"]
    },
    {
      "type": "object",
      "properties": {
        "ok": {"const": false},
        "error": {"type": "string"},
        "summary": {"type": "string"}
      },
      "required": ["ok", "error", "summary"]
    }
  ]
}
```

Every contract must include `"ok": boolean` and `"summary": string` at minimum. The summary is human-readable — it's what shows up in the receipt.

## main.py — the entrypoint

### Protocol: json-stdio

The Drone reads ONE JSON object from stdin and writes ONE JSON object to stdout. That's the entire interface.

```python
import json
import sys

def main():
    data = json.loads(sys.stdin.read())
    goal = data.get("goal", "")
    workspace_root = data.get("workspace_root", ".")
    
    # ... do the work ...
    
    result = {"ok": True, "summary": "...", ...}
    print(json.dumps(result))

if __name__ == "__main__":
    main()
```

### Rules for main.py

1. **Single verb.** The Drone does one thing. If you're tempted to add a mode switch or a flag that changes behavior, you need two Drones.

2. **Parse the goal string for parameters.** The goal is prose from the planner or user. Extract structured values from it using simple line parsing (e.g. `target_file: path/to/file`). Document what goal lines you expect in the manifest's `instructions` field.

3. **Workspace-relative paths only.** Resolve everything against `workspace_root`. Validate that resolved paths don't escape the workspace (path traversal check).

4. **Fail loudly, fail structured.** Every error path returns `{"ok": false, "error": "...", "summary": "..."}`. Never crash to stderr with no stdout — the runner reads stdout.

5. **No side effects unless write_policy allows it.** A `read_only` Drone must not modify files, push to git, make HTTP POST/PUT/DELETE requests, or mutate any state. It reads, computes, and reports.

6. **Stdout is sacred.** Only the final JSON result goes to stdout. Debug output, progress, logging → stderr. `print()` is stdout — use it exactly once, at the end, for the result.

7. **No external dependencies unless declared.** Stick to the Python standard library. If you need a third-party package, note it in the manifest's `dependencies` array AND document it in `instructions`. The runner doesn't pip-install for you (yet).

8. **Timeout-aware.** The budget gives you N seconds. For operations that could hang (subprocess, network), set timeouts shorter than the budget. A Drone that times out is killed — make sure partial work doesn't leave corruption.

9. **Idempotent when possible.** Running the same Drone twice on the same input should produce the same output (or at minimum, not corrupt state). This matters for chains that retry.

## Design principles

- **The receipt is the proof.** Every Drone's output is a receipt that proves what happened. Include enough detail that a human reading the receipt can judge whether the work was correct WITHOUT re-running the Drone.

- **Typed over prose.** Contracts, output fields, error shapes — use typed schemas everywhere. Prose descriptions are for humans reading the manifest; schemas are for machines routing the data.

- **Chainable by default.** Your `cargo_contract` is another Drone's input. Make the output shape clean enough that a downstream Drone can consume it without parsing prose out of a summary string.

- **Small is right.** A 50-line Drone that does one thing perfectly is better than a 300-line Drone that handles three cases. Split the cases into three Drones and chain them.

## Reference: good Drone (Remora Snapshot)

This Drone captures a file baseline — hash, line count, git state, parse validity, optional test run. Study it as the reference for how to build.

Key things it does right:
- Typed JSON Schema contracts on input, output, and cargo
- Single verb: snapshot (read-only, no mutations)
- Goal string parsing for parameters (`target_file:`, `test_command:`)
- Path traversal protection
- Structured error responses on every failure path
- subprocess timeouts shorter than the budget
- Clean separation: each concern is its own function
- Summary string that tells a human what happened