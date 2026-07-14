---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**", "scripts/live/**", ".aura/tools/**"]
triggers: ["aura preview", "godot bridge", "build_live_ruin", "procedural construction", "assemble", "godot live", "live scene", "live composition"]
---
### Godot Live Building — Direct Exact Pieces

Exact modular catalog pieces are the normal construction medium for V_Ruins. Use the same basic actions a person uses in the Godot editor: place an exact piece, duplicate pieces into runs or vertical courses, move, rotate, attach through sockets, replace, and remove.

#### Interactive Mode

- Use one Interactive Mode for live building. Do not introduce another Planner path, execution mode, bridge, architecture engine, grammar, template system, named archetype, generator, critic, or scoring system.
- Aura owns composition, scale, sequencing, exact asset selection, wall lengths, tower footprints, height, openings, asymmetry, damage, and revision.
- Godot owns catalog resolution, node creation, UndoRedo, socket attachment calculations, validated rotations, and preview mutation.
- Use `edit_godot_asset_preview` as the primary mutation tool for ordinary V_Ruins creative construction.
- Never save the scene unless the user explicitly requests it. Each direct revision is one atomic Godot UndoRedo action.

Use this normal rhythm:

`inspect_godot_assets once when needed → inspect_godot_asset_preview when needed → select exact asset IDs → edit_godot_asset_preview with one cohesive direct revision → read changed-piece facts → continue with another cohesive revision → capture_godot_asset_preview at a useful visual checkpoint → return control`

Apply one cohesive direct editor revision at a time, then use its returned facts to choose the next cohesive revision.

A cohesive revision is a useful batch, not one model call per mesh piece. It may contain:

- one wall followed by a duplicated wall run;
- a run plus its ending corner;
- one complete wall course or a vertical stack of several courses;
- the four sides and corners of a substantial tower base;
- replacements that introduce windows, openings, or damaged variants;
- moves and removals for pieces that do not read correctly.

Use `instantiate`, `duplicate`, `attach`, `set_transform`, `replace`, and `remove` directly. Use calibrated `relative_to` anchor planes for structural walls, corners, floors, and vertical courses. Use calibrated duplicate stepping for repeated runs and courses, and compatible sockets where they provide an exact connection. Bounded positions, offsets, and verified yaw rotations are valid; raw coordinates are mainly for the first anchor, deliberate free placement, rubble, and decoration. Never manually calculate asset pivot corrections; apply only small intentional offsets after calibrated alignment.

Inspect the returned exact transforms and continue from changed-piece facts. Build a tall tower through its footprint and repeated vertical courses instead of stretching wall pieces unnaturally. Preserve and repair the current preview rather than rebuilding it automatically.

Read the returned compact facts after every revision: operation and total instance counts, added/changed/replaced/removed paths, affected exact asset IDs and domains, current transforms, useful sockets, preview bounds, and conservative placement warnings. Overlap warnings are informational; intentional masonry intersections, wall embedding, rubble, floors meeting walls, and incomplete work remain valid.

#### Composition Ownership

- Do not require named rooms, mass maps, supported spans, vertical profiles, space relationships, or semantic handles before Aura can manipulate exact pieces.
- Do not automatically inspect mass maps, vertical profiles, structural continuation candidates, or styling affordances during direct construction.
- Towers, gatehouses, keeps, naves, bridges, castles, monasteries, and similar concepts are compositions Aura creates from pieces. Do not create named architectural operations for them.
- Preserve the user's corrections and strong existing work. Continue modifying the current `AuraPreview` instead of rebuilding from scratch.
- Direct exact pieces and existing `AuraProc__...` semantic pieces may coexist beneath the same `AuraPreview`. Do not migrate or rename semantic preview nodes.

#### Optional Semantic Compatibility

`build_live_ruin` remains available as an optional shortcut, not a prerequisite or default. Use it only when its existing semantic vocabulary is genuinely useful, such as quickly laying a basic enclosure, floor, stair connection, supported span, or calibrated wall-face feature. Every shortcut result remains ordinary editable preview pieces that can be continued, moved, replaced, or removed through `edit_godot_asset_preview`.

When using a shortcut, its returned handles, topology, mass maps, vertical profiles, and diagnostics may guide the next semantic shortcut. Keep semantic construction compatible, atomic, and unsaved, but do not impose those facts on the direct exact-piece workflow.

#### Visual Checkpoints and Handoff

- Capture only when the current work reaches a useful visual checkpoint or the user asks to look.
- A screenshot is only a capture. Claim visual quality only from actual vision findings.
- Without vision findings, report factual changes and let the user judge the preview.
- Return control after a meaningful component or useful checkpoint so the user can redirect the existing work.

#### Safety and Scope

- Use exact catalog asset IDs; never inject arbitrary resource paths.
- Respect catalog identity checks, finite numeric bounds, verified yaw rotations, socket constraints, total preview capacity, and UndoRedo validation.
- Reject mechanical failures such as unknown assets, missing sources, invalid names/transforms, impossible attachments, forbidden rotation, out-of-bounds placement, or capacity overflow.
- Do not turn overlap, embedding, incomplete construction, silhouette, hierarchy, or architectural taste into hard rejection.
- Do not use delayed playback, animation, probe geometry, or one model call per piece.
