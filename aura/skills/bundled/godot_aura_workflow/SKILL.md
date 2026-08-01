---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**"]
triggers: ["aura preview", "godot bridge", "inspect_godot_api", "inspect_godot_assets", "capture_godot", "visual iteration", "assemble", "edit_godot_asset_preview", "godot live", "live scene", "live composition"]
workspace_markers: ["project.godot"]
---
### Godot Visual Iteration

The production agent owns inspection, mutation, evidence, and revision for the live-editor task.

#### Composition contract

- Discover assets with `inspect_godot_assets` and place them by catalog asset ID through
  `edit_godot_asset_preview`; do not substitute raw resource paths.
- Keep every composition node under the genuine `AuraPreview` root. Inspect live state with
  `inspect_godot_editor` and `inspect_godot_asset_preview`. Never save the scene unless the user
  explicitly requests saving.
- Build in visible layers: footprint and landmarks; connected structural runs and entrances;
  secondary spaces; then damage, rubble, props, and storytelling. Prefer small passes.

#### Evidence loop

After each pass, inspect exact scene facts, capture a view with
`capture_godot_asset_preview`, and judge the result against the original brief. When available, call
`critique_godot_preview_local` with the capture, brief, and scene facts. Revise the worst coherence
failure while preserving the strongest feature, then inspect and recapture.

Use snapshots and diagnostics for structural facts such as transforms, dimensions, instance counts,
ownership, and overlap. Use captures and semantic critique for visual hierarchy, silhouette,
readability, connectedness, negative space, atmosphere, and storytelling. Structural facts alone do
not prove visual coherence. A `needs_revision` verdict requires another focused pass;
`cannot_judge` requires a better capture. Claim coherence only when useful visual evidence supports it
and structural facts do not contradict it. If visual critique is unavailable, report that limitation.

Continue while each pass has a clear purpose and produces meaningful improvement. Stop when the brief
is coherently satisfied, the user cancels, evidence stops changing, no useful improvement remains, or
a tool failure prevents further work.
