---
task_kinds: ["validation", "test", "godot_validation"]
path_globs: []
triggers: ["godot validation", "godot test", "headless", "script test", "script error", ".uid", "class registration", "class_name", "import", "verify the project"]
workspace_markers: ["project.godot"]
---
### Godot Validation

- Use the repository's real configured or discovered Godot executable and confirm its version; do not
  silently substitute a generic binary or assumptions from another Godot release.
- When script tests depend on global class registration or imported resources, run a focused headless
  editor import first and let it finish. This import generates required `.uid` files before tests parse
  dependent scripts.
- Run the narrowest headless check or repository test target that exercises the change. Inspect all
  output for `SCRIPT ERROR` and `ERROR:` even when Godot exits with code 0.
- Do not count process launch, a timeout, an early parse failure, or a missing test summary as test
  completion. Require the intended runner's completion signal or result count, plus clean Godot output,
  before reporting success.
