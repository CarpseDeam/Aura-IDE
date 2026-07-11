---
task_kinds: ["3d", "level design", "godot_3d"]
path_globs: ["**/*.tscn", "assets/**"]
triggers: ["node3d", "transform3d", "packedscene", "modular", "assembly", "placement", "socket", "local transform", "global transform", "level"]
---
### Godot 3D and Modular Assembly

- Distinguish local `transform`/`position` from `global_transform`/`global_position`. Parent transforms
  affect descendants unless `top_level` is deliberately enabled. Convert points with `to_local()` and
  `to_global()` instead of hand-subtracting origins.
- Keep scale components nonzero and consistently signed. Prefer authored asset scale plus placement
  calibration over compensating for import problems throughout gameplay code.
- Treat a `PackedScene` as a reusable component boundary. Instantiate, add it to the intended parent,
  then set owner correctly when the edit must serialize in the editor.
- For modular kits, use explicit catalog dimensions, pivots, allowed rotations, and semantic sockets.
  Pixels may critique appearance, but semantic metadata controls identity, alignment, collision, and
  connectivity.
- Plan in a disposable preview parent first. Inspect the exact live tree after each batch, validate
  structure, capture controlled views, and revise with one bounded UndoRedo action. Never save
  implicitly.
- Do not flatten imported scene internals or rewrite source assets merely to place them. Prefer wrapper
  scenes or calibration metadata when an asset needs project-specific behavior.
