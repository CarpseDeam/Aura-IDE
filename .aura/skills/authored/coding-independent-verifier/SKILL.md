---
task_kinds: ["coding", "test"]
path_globs: []
model: null
triggers: ["verify", "verifier", "validation", "test", "expected", "actual"]
---

The thing that verifies must not share state or derivation with the thing it verifies. No function checking its own output. No test restating the implementation. Entangled verifier and verified make the green prove nothing.
