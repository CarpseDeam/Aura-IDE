---
task_kinds: ["coding"]
path_globs: []
model: null
triggers: ["exception", "except", "try", "return", "success", "postcondition"]
---

Do not catch an exception and return a normal-looking result. Either repair state so the postcondition holds, return an explicit failure shape if the caller expects one, or re-raise. A swallowed error with a success-shaped return is a silent bug.
