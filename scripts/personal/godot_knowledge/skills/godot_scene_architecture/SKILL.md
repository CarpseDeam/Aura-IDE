---
task_kinds: ["architecture", "scene", "godot_scene"]
path_globs: ["**/*.tscn", "project.godot"]
triggers: ["godot", "scene tree", "node", "signal", "group", "resource", "autoload", "singleton", "dependency injection"]
---
### Godot Scene Architecture

Treat a reusable scene as a focused component with a clear root and as few assumptions about its
parent or siblings as possible. Prefer composition of small scenes and Resources over one script or
scene that owns unrelated systems. A parent/owner should wire dependencies between children.

- Use signals for upward/outward events, especially completed facts (`died`, `entered`,
  `item_collected`), and direct method calls for commands when the dependency is explicit.
- Use exported references, typed Resources, constructor/setup methods, or NodePaths supplied by the
  owning scene instead of brittle absolute paths and deep sibling traversal.
- Use groups as semantic tags and broadcast/query surfaces; never depend on group iteration order.
- Use an autoload only for genuinely broad, self-contained lifetime or service state. Do not create a
  generic global manager merely to make references convenient.
- Keep runtime/world, persistent session, and UI branches separate when their lifetimes differ.
- Before restructuring an established project, inspect its existing scene conventions, autoloads,
  groups, and ownership. Preserve local architecture unless the request explicitly authorizes a
  migration.
- For required editor configuration, prefer `_get_configuration_warnings()` on a tool script so the
  scene explains invalid setup where authors see it.

Avoid god scripts, generic Manager dumping grounds, hidden global state, and scenes that only work at
one exact tree path.
