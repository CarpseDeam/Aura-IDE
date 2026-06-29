---
task_kinds: ["coding", "refactor"]
path_globs: []
model: null
triggers: ["refactor", "god file", "manager.py", "seam", "cleanup", "scope", "drive-by"]
---

Extract or repair one real seam at a time. Do not broadly clean up god files. Do not touch unrelated GUI, provider, drone, release, README, or version files unless explicitly requested. Preserve behavior unless behavior change is the goal.
