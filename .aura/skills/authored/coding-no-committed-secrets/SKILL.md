---
task_kinds: ["coding"]
path_globs: []
model: null
triggers: ["api_key", "apikey", "Authorization", "Bearer", "secret", "token", "credential", "password"]
---

Do not write credentials, API keys, or tokens as string literals. Read them from environment or config at runtime. Nothing secret gets committed.
