---
task_kinds: ["coding"]
path_globs: []
model: null
triggers: ["subprocess", "os.system", "Popen", "shell=True", "command", "cmd"]
---

Do not build a shell command by formatting variables into a string or pass shell=True with interpolated input. Use an argument list. This is command injection regardless of where the input came from.
