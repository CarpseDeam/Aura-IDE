---
task_kinds: ["coding", "refactor"]
path_globs: []
model: null
triggers: ["io", "file", "network", "subprocess", "database", "request", "response", "pure function"]
---

Do not fuse I/O with decision logic in one function. It becomes untestable without the I/O. Put the decision in a pure function over data; keep file, network, subprocess, database, and UI I/O at the edge.
