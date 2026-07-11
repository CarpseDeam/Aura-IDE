---
task_kinds: ["visual iteration", "godot_bridge"]
path_globs: ["addons/aura_bridge/**"]
triggers: ["aura preview", "godot bridge", "inspect_godot_api", "inspect_godot_assets", "capture_godot", "visual iteration", "assemble"]
---
### Aura Godot Tool Workflow

Use evidence in this order:

1. Inspect project code/scene conventions and the live editor state.
2. Query `inspect_godot_api` for uncertain engine signatures; do not invent APIs.
3. Query `inspect_godot_assets` before choosing reusable project assets.
4. Assemble only catalog-approved scenes beneath `AuraPreview`.
5. Run `inspect_godot_asset_preview`; structural facts outrank image interpretation.
6. Run `capture_godot_asset_preview` for controlled visual evidence.
7. When available, use `critique_godot_preview_local` for bounded aesthetic observations only.
8. Apply a small atomic preview revision, reinspect, and stop within the user's pass bound.

Use exact live preview paths for transform/remove/replace operations. Keep every mutation approval
gated and undoable. Do not route catalog scenes through arbitrary path arguments, edit nodes outside
the preview boundary, start an unbounded critic loop, or save the scene unless explicitly requested.
