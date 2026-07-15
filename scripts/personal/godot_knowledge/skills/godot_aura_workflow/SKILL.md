---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**", "scripts/live/**", ".aura/tools/**"]
triggers: ["aura preview", "godot bridge", "build_live_ruin", "procedural construction", "assemble", "godot live", "live scene", "live composition"]
---
### Godot Live Building — Procedural Co-Building

Use the existing semantic workflow to build the place the user actually requested. `build_live_ruin` is the primary mutation tool. `edit_godot_asset_preview` is only a narrow fallback for a requested catalog edit that the semantic vocabulary cannot express.

For a small bounded edit:

`one instruction → one build_live_ruin call → short factual receipt → wait`

For a broader request:

`inspect once if needed → build the defining feature → read returned facts → continue the connected structure → useful checkpoint → user handoff`

#### Interactive Mode

- Use one Interactive Mode for live building. Small edits and progressive builds are workflows selected by instruction scope, not separate execution modes.
- Read the user's requested place literally. Start with its defining feature, such as a gate passage, hall, courtyard, tower, room, or wall, so later construction grows from something recognizable.
- Build connected architecture with the existing semantic operations. Add floors, walls, openings, upper levels, ceilings, and stairs where the requested structure or physical access requires them.
- Inspect `inspect_live_ruin_contract` at most once per request when the contract or current semantic state is unknown. After it returns, make the first useful `build_live_ruin` call immediately.
- Put exactly one cohesive semantic operation in each `build_live_ruin` call. The model must receive that call's completed post-apply state before choosing the next operation; do not submit future live-build calls beside it.
- Continue beneath the existing real `AuraPreview`. Later requests such as `extend the east room` or `breach the rear wall` modify the named live structure and preserve successful earlier work.
- Use returned piece count, handles, spaces, levels, walls, openings, connections, created or modified references, and geometry or topology diagnostics as simple facts. Choose what to build next from the user's request and the structure already present.
- Do not treat returned metadata, operation names, or successful validation as proof that the requested architecture is complete or visually successful.
- Do not inspect constructor source, catalog files, exact node transforms, or implementation details during ordinary semantic construction when the contract supplies the needed syntax and references. Never create probe geometry to learn behavior.
- Never save the scene unless explicitly requested. Each procedural call is one atomic Godot `UndoRedo` action.

#### Connected Construction

- Complete one meaningful requested component or useful checkpoint at a time. For a large place such as a citadel, castle, fortress district, monastery, or multi-zone ruin, use several connected `build_live_ruin` calls rather than one comprehensive call.
- Keep building inside the current request while another operation is plainly needed for the component being built. Do not pause after every wall or module, and do not discard successful earlier calls when a later operation is rejected.
- If a call fails, use its exact structured diagnostic and valid corrective candidates to fix only that operation. Geometry and topology failures do not require vision or individual mesh nudges.
- If an API interruption follows a successful step, reconstruct current state with `inspect_live_ruin_contract`, continue from returned handles, and never repeat an already-present stable handle.
- Do not add an elevated bridge, span, connector, upper chamber, or joining structure unless the user explicitly requested one or it is plainly necessary for physical access.
- Use `add_supported_span` only when the user explicitly asks for an elevated bridge, skyway, upper passage, or another structure joining separate supports. Two spaces at the same level are not a reason to add one.

#### Procedural Vocabulary

- `create_run`, `turn_run`, and `extend_run` build connected wall runs from semantic anchors and cardinal directions.
- `create_enclosure`, `attach_room`, and `extend_room` build or extend rooms and enclosures from named walls and slots.
- `add_floor_region`, floor and ceiling options, `add_upper_level`, and `add_upper_wall_section` add the surfaces or upper construction the request needs.
- `insert_opening`, `connect_spaces`, `add_approach`, and `add_stair_run` provide entrances, passages, and physical circulation.
- `place_wall_piece` selects an exact catalog `asset_id` for an existing wall face. Tags may filter catalog results but never silently select an asset; project calibration owns transforms, orientation, embed, and backing-wall placement.
- `apply_damage` and the existing surface-damage operations revise selected existing construction without rebuilding unrelated work.
- `add_supported_span` remains available for the explicit joining cases described above; it is not a normal completion step.

#### Planner and Worker Role Split

##### Planner (read-only)

- Preserve the user's literal request and no-save instruction in one compact Worker item.
- Name `build_live_ruin` as the primary mutation tool.
- Use `inspect_godot_assets`, `inspect_godot_editor`, or `inspect_godot_asset_preview` only when their facts are actually needed. Do not attempt mutations, read bridge credentials, prescribe raw resource paths, or author another execution path.

##### Worker (owns every mutation)

- Use `inspect_live_ruin_contract` once when needed, then use `build_live_ruin` with semantic parameters and stable handles.
- Use `edit_godot_asset_preview` only as the narrow fallback described above.
- Keep every generated piece beneath the genuine `AuraPreview`, every call atomic, and the scene unsaved unless the user explicitly permits saving.

#### Visual Checkpoints

- Do not automatically call `capture_godot_asset_preview` or `critique_godot_preview_local`, start an unsolicited refinement loop, or capture merely to prove a semantic edit succeeded.
- When the user asks for visual judgment or a genuinely visual question remains, inspect exact scene facts, capture a useful view with `capture_godot_asset_preview`, and use a vision-capable tool such as `critique_godot_preview_local` when available.
- Capturing an image alone is not visual analysis. Never claim visual coherence or quality without visual findings, and do not make vision a mandatory gate between semantic construction steps.

#### Safety and Scope

- Use catalog-only asset IDs and project calibration; no arbitrary `.tscn` paths or raw transforms.
- No raw TCP, bridge tokens, second bridge, helper generator, autonomous builder state machine, templates, named building generators, new architecture engine, scoring, critic gate, or mandatory vision.
- Invalid geometry must reject before mutation. A failed call applies nothing from that call; successful earlier calls remain in the unsaved preview.
- Never save the scene unless explicitly requested.
