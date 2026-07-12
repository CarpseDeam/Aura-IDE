---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**", "scripts/live/**", ".aura/tools/**"]
triggers: ["aura preview", "godot bridge", "build_live_ruin", "procedural construction", "assemble", "godot live", "live scene", "live composition"]
---
### Godot Live Building — Procedural Co-Building

The primary structural loop is:

`inspect once → choose semantic operations → build_live_ruin applies one deterministic batch → immediately choose the next structural operation`

#### Interactive Mode

- DeepSeek chooses structural intent; project code owns exact mesh positions, rotations, spacing, socket alignment, corner selection, opening widths, occupancy checks, and stable names.
- Prefer one `build_live_ruin` call containing an ordered batch of meaningful operations. Refer to returned stable handles in later calls.
- Express footprint dimensions, cardinal directions, module counts, entrance sides, opening slots, attached sides, room dimensions, tower anchors, and selected damage sections. Never calculate a transform for every mesh.
- Build incrementally beneath the existing real `AuraPreview`. Later requests such as `extend the east room` or `breach the rear wall` modify the named live structure.
- Continue within the current request while another requested structural operation remains. Do not pause after every wall or module.
- Never save the scene unless explicitly requested. Each procedural call is one atomic Godot UndoRedo action.

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
- Produces one compact Worker item for the complete live procedural composition request, preserving the user’s semantic intent and no-save instruction.
- Names `build_live_ruin` as the primary mutation tool.
- Must not attempt mutations, read bridge credentials, prescribe raw resource paths, or author another execution path.

##### Worker (owns every mutation)

- Uses catalog facts and `build_live_ruin` with semantic parameters and stable handles.
- Uses `edit_godot_asset_preview` only as a narrow fallback for a catalog operation the project vocabulary does not support; it is not the normal structural path.
- Keeps every generated piece beneath the genuine `AuraPreview` and never saves unless explicitly requested.
- On a rejected procedural batch, fix the semantic request or report the concrete geometry error. Do not nudge individual mesh transforms.

#### Visual Checks Are Isolated

- Do not call `capture_godot_asset_preview` or local vision during ordinary structural placement, and do not make visual description control progression.
- Production capture infrastructure remains available. An occasional later composition check may use `capture_godot_asset_preview` and `describe_godot_preview_local` only when the user requests it or a genuinely visual question remains after deterministic construction.
- Do not turn that optional check into critique, scoring, a verdict gate, an autonomous loop, or repeated correction passes.

#### Safety and Scope

- Use catalog-only asset IDs and project calibrations; no arbitrary `.tscn` paths.
- No raw TCP, bridge tokens, second bridge, helper generator authored during a live composition, autonomous builder state machine, grammar engine, WFC, critic, or scoring system.
- Invalid geometry must reject before mutation; never accept a half-applied structure.
- Never save the scene unless explicitly requested.
