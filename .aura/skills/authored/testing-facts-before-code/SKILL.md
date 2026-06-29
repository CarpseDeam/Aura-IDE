---
task_kinds: ["test", "coding"]
path_globs: ["test_*.py", "tests/**"]
model: null
triggers: ["test", "expected", "assert", "fixture", "repro", "contract"]
---

Write tests against a fact that predates the code: a bug repro, declared contract, known value, fixture, protocol, or user-visible requirement. Never restate the implementation. Code-derived tests only prove the code does what it does.
