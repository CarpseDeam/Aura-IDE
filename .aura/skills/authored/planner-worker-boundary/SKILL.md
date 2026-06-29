---
task_kinds: ["planning", "coding"]
path_globs: ["aura/bridge/**", "aura/context_gearbox/**", "aura/conversation/tools/**"]
model: null
triggers: ["planner", "worker", "dispatch", "task capsule", "handoff", "implementation reasoning"]
---

Planner compresses; Worker expands. Planner owns user intent, target seam, allowed files, constraints, non-goals, and validation expectations. Planner must not write code, sketch patches, plan exact hunks, or do implementation reasoning. Worker owns file reads, exact edits, implementation reasoning, validation, code quality decisions, and final summary.
