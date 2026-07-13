---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**", "scripts/live/**", ".aura/tools/**"]
triggers: ["aura preview", "godot bridge", "build_live_ruin", "procedural construction", "assemble", "godot live", "live scene", "live composition"]
---
### Godot Live Building — Procedural Co-Building

The primary rapid-supervision loop is:

`inspect once → dispatch one compact Worker item → semantic construction returns exact facts → report and wait for the next direction`

#### Interactive Mode

- DeepSeek chooses structural intent; project code owns exact mesh positions, rotations, spacing, socket alignment, corner selection, opening widths, occupancy checks, and stable names.
- Prefer one `build_live_ruin` call containing an ordered batch of meaningful operations. Refer to returned stable handles in later calls.
- Express footprint dimensions, cardinal directions, module counts, entrance sides, opening slots, attached sides, room dimensions, tower anchors, and selected damage sections. Never calculate a transform for every mesh.
- Build incrementally beneath the existing real `AuraPreview`. Later requests such as `extend the east room` or `breach the rear wall` modify the named live structure.
- Continue within the current request while another requested structural operation remains. Do not pause after every wall or module.
- Never save the scene unless explicitly requested. Each procedural call is one atomic Godot UndoRedo action.

#### Rapid Supervised Construction

- This is the normal behavior while the user directs successive bounded edits such as adding or removing a room, level, stairs, opening, or connection; extending a hall; raising a wall or named space; or damaging a selected section.
- Dispatch one compact Worker item for the current instruction. Use the existing semantic project-local construction tool, such as `build_live_ruin`, when available.
- Trust the tool's returned snapshot reconstruction, validation, topology, stable handles, and atomic revision result as structural truth.
- Do not automatically call `capture_godot_asset_preview` or `critique_godot_preview_local`, start an unsolicited visual refinement loop, or capture merely to prove that a semantic edit succeeded.
- Return a short factual receipt of the structural result and wait for the user's next direction. Do not claim visual coherence or quality without visual evidence.
- If the semantic operation fails, use its exact geometry or topology diagnostics first. Vision is not the default recovery path for structural failure.

#### Procedural Vocabulary

- `create_run`: named straight run from a semantic start anchor, cardinal direction, module count, and catalog piece family.
- `turn_run`: correctly selected and oriented corner from a named run into a new cardinal direction.
- `extend_run`: add deterministic modules to an existing named run.
- `create_enclosure`: connected four-sided enclosure with footprint/module dimensions and a deliberate entrance.
- `insert_opening`: replace a named run slot with a doorway, breach, gap, damaged section, or intact wall.
- `attach_room` and `extend_room`: add or extend a named secondary enclosure from a named wall slot.
- `add_tower`: replace a compatible named corner with a heavier/taller catalog corner, or add a catalog mass at a named wall anchor.
- `apply_damage`: deterministically replace selected intact run slots with compatible damaged catalog variants.

#### Planner and Worker Role Split

##### Planner (read-only)

- May inspect project conventions, catalog (`inspect_godot_assets`), live scene (`inspect_godot_editor`), and AuraPreview (`inspect_godot_asset_preview`) once at the start when facts are unknown.
- Produces one compact Worker item for the current live procedural construction request, preserving the user’s semantic intent and no-save instruction.
- Names `build_live_ruin` as the primary mutation tool.
- Must not attempt mutations, read bridge credentials, prescribe raw resource paths, or author another execution path.

##### Worker (owns every mutation)

- Uses catalog facts and `build_live_ruin` with semantic parameters and stable handles.
- Uses `edit_godot_asset_preview` only as a narrow fallback for a catalog operation the project vocabulary does not support; it is not the normal structural path.
- Keeps every generated piece beneath the genuine `AuraPreview` and never saves unless explicitly requested.
- On a rejected procedural batch, fix the semantic request or report the concrete geometry error. Do not nudge individual mesh transforms.

#### Visual Checkpoints and Styling Work

- Use visual evidence when the user explicitly asks to take a look, inspect the composition, or judge how it reads, or when the task genuinely depends on visual judgment: styling, silhouette, imposing character, believable damage, or autonomous broad design and refinement.
- Inspect exact scene facts, capture a useful view with `capture_godot_asset_preview`, and call `critique_godot_preview_local` when it is installed and callable. Make focused revisions when requested.
- Never claim visual coherence or quality without useful visual evidence. Capture and critique are task-driven checkpoints, not mandatory proof after each construction mutation.

#### Safety and Scope

- Use catalog-only asset IDs and project calibrations; no arbitrary `.tscn` paths.
- No raw TCP, bridge tokens, second bridge, helper generator authored during a live composition, autonomous builder state machine, grammar engine, WFC, critic, or scoring system.
- Invalid geometry must reject before mutation; never accept a half-applied structure.
- Never save the scene unless explicitly requested.
