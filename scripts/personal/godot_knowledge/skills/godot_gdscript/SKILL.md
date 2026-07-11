---
task_kinds: ["bugfix", "gdscript"]
path_globs: ["**/*.gd"]
triggers: ["gdscript", "godot api", "await", "signal", "typed", "export", "ready", "process", "physics process"]
---
### Godot 4.6 GDScript Practice

- Use Godot 4 syntax and typed declarations where they clarify contracts. Treat warnings-as-errors as
  real validation: explicitly type values returned as `Variant` instead of relying on `:=` inference.
- Query `inspect_godot_api` before relying on an uncertain engine class, method, property, signal,
  enum, argument order, or default. ClassDB is exact for the running editor version. Search project
  code for script-defined `class_name` types.
- Use `_physics_process(delta)` for fixed-step physics decisions and `_process(delta)` only for work
  that truly needs every rendered frame. Disable processing when idle rather than polling forever.
- Resolve required children deliberately (`@onready` or exported references), validate optional
  nodes/resources, and avoid repeated `get_node()` calls in hot paths.
- Connect signals once at a clear ownership boundary. Guard against duplicate connections and
  callbacks into freed objects when lifetimes differ.
- Use `queue_free()` for normal SceneTree-owned deletion. After deferred operations or `await`, assume
  referenced nodes may have left the tree; revalidate when necessary.
- Keep data in typed Resources when it must be shared, authored, duplicated, or serialized separately
  from behavior.
- Validate with Godot itself and inspect output for `SCRIPT ERROR`/`ERROR:` because some headless
  checks can still return exit code 0 after a parse failure.
