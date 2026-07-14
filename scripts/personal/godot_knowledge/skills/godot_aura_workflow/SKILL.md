---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**", "scripts/live/**", ".aura/tools/**"]
triggers: ["aura preview", "godot bridge", "build_live_ruin", "procedural construction", "assemble", "godot live", "live scene", "live composition"]
---
### Godot Live Building — Procedural Co-Building

For a small bounded edit, use the fast loop:

`one instruction → one build_live_ruin call → short receipt → wait`

Its supervision summary remains: `inspect once → dispatch one compact Worker item → semantic construction returns exact facts → report and wait for the next direction`.

For a broad creative place, use one progressive pass that completes one major building to a visual checkpoint before another begins:

`inspect once if needed → meaningful mass → live state → height and silhouette → walls and openings → localized ruin character → visual checkpoint → user handoff`

#### Interactive Mode

- Use one Interactive Mode for live building. Small-edit and progressive large-build behavior are workflows selected by instruction scope, not separate execution modes.
- DeepSeek chooses structural intent; project code owns exact mesh positions, rotations, spacing, socket alignment, corner selection, opening widths, occupancy checks, and stable names.
- Classify the current instruction by scope. Keep one wall run, bounded room, floor region, level, tower section, stair, opening, connection, bounded facade motif, extension, or damage edit to one `build_live_ruin` call containing one cohesive semantic operation.
- Treat broad, multi-zone creative requests as progressive builds. Dispatch one compact Worker item for the requested pass, then make several result-driven `build_live_ruin` calls inside that same Worker item.
- Express footprint dimensions, cardinal directions, module counts, entrance sides, opening slots, attached sides, room dimensions, tower anchors, and selected damage sections. Never calculate a transform for every mesh.
- Build incrementally beneath the existing real `AuraPreview`. Later requests such as `extend the east room` or `breach the rear wall` modify the named live structure.
- Continue within the current request while another requested structural operation remains. Do not pause after every wall or module.
- Never save the scene unless explicitly requested. Each procedural call is one atomic Godot UndoRedo action.

Use this action-first rhythm inside Interactive Mode:

- For live procedural construction, call `inspect_live_ruin_contract` at most once per request when the contract or current semantic state is unknown. Treat the contract and returned semantic state as authoritative.
- After the contract returns, make the first useful `build_live_ruin` call immediately. Do not inspect V_Ruins constructor source, catalog files, exact node transforms, or implementation details before that call or during ordinary semantic construction when the contract is available.
- Prefer a coarse but valid, meaningful architectural chunk that can be revised over prolonged preflight design intended to perfect the whole zone before applying anything. Use visible iteration as the planning mechanism: apply a meaningful chunk, observe its returned semantic result, then apply the next chunk.
- Emit exactly one `build_live_ruin` call in each assistant tool-call round and put exactly one cohesive semantic operation in that call. The model must receive the completed call's compact post-apply state before choosing or submitting the next construction step; do not place future live-build calls beside it in the same assistant message.
- After a successful build call, continue directly to the next `build_live_ruin` call from its returned handles, spaces, connections, and diagnostics without another inspection. Do not inspect the preview to determine exact coordinates for the next semantic operation.
- For broad creative work, also use the returned styling affordances to keep authoring the current building. Unless the user explicitly requests blockout or foundation only, requested rooms and volumes do not by themselves complete the instruction.
- Develop one major building through structural massing, vertical and silhouette shaping, facade and opening articulation, coherent localized ruin treatment, and a useful visual checkpoint before moving to another building or reporting completion.
- After each major mass, evaluate relevant existing general operations including upper wall sections, raised courses, upper levels, entrance features, framed or inserted openings, colonnades, arcades, ceiling arches or remnants, and surface or wall damage. Choose only combinations suited to the requested character, scale, function, and mood.
- Do not force symmetry, fixed proportions, or fixed motif sequences. Compose original results from verified walls, tall corners, windows, doors, pillars, arches, floors, stairs, broken variants, vegetation, and lighting without introducing named builders, templates, or new assets.
- Perform another read-only inspection only when a concrete structured diagnostic cannot be resolved from its returned valid candidates. Never create probe geometry to learn behavior.
- If an API interruption ends the turn after a successful step, reconstruct current state with `inspect_live_ruin_contract` and continue from returned handles. Never repeat an already-present stable handle or recreate geometry to catch up.

#### Rapid Supervised Construction

- This is the normal behavior while the user directs successive bounded edits such as adding or removing a room, level, stairs, opening, or connection; extending a hall; raising a wall or named space; or damaging a selected section.
- Dispatch one compact Worker item for the current instruction. Use the existing semantic project-local construction tool, such as `build_live_ruin`, when available.
- Trust the tool's returned snapshot reconstruction, validation, topology, stable handles, and atomic revision result as structural truth.
- Do not automatically call `capture_godot_asset_preview` or `critique_godot_preview_local`, start an unsolicited visual refinement loop, or capture merely to prove that a semantic edit succeeded.
- Return a short factual receipt of the structural result and wait for the user's next direction. Do not claim visual coherence or quality without visual evidence.
- If the semantic operation fails, use its exact geometry or topology diagnostics first. Vision is not the default recovery path for structural failure.

#### Progressive Large Construction

- Inspect `inspect_live_ruin_contract` once when the semantic contract or current handles are unknown. Treat its operation schemas, grammar, live reconstruction, and valid candidates as authoritative.
- Do not inspect project source code to discover semantic operation syntax when the contract tool is available. Do not create disposable probe walls, rooms, or openings to infer coordinates, handles, anchors, attachment forms, or naming conventions.
- Break the requested place into meaningful connected semantic steps within the same Worker item, but complete the current major building's mass, silhouette, articulation, and localized damage before blocking out another building.
- Do not force the entire place into one comprehensive `build_live_ruin` call. Apply each successful zone immediately with its own atomic `build_live_ruin` call so progress becomes visible.
- Read the compact post-apply handles, created or modified spaces, piece-count delta, openings, connections, styling affordances, and validation diagnostics after each successful call and use them as references in the next step. Do not guess a handle that the prior call did not return.
- Do not pause for user input between zones while requested structural work remains. Do not return a receipt after each zone; return one concise final receipt after the requested pass completes or a real semantic failure prevents continuation.
- Do not call `capture_godot_asset_preview`, `critique_godot_preview_local`, or any vision tool between semantic construction steps. Capture once the current building reaches a useful visual checkpoint, then return control so the user can direct the next addition.
- If one zone fails, keep prior successful zones, use the structured diagnostic to correct only the failed zone, and retry that zone. Never request partial application of a failed atomic call and do not add speculative retry machinery.

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

- During ordinary semantic construction, use `inspect_live_ruin_contract` at most once when needed and dispatch the first useful build without catalog, scene, source, transform, or implementation preflight. Reserve catalog (`inspect_godot_assets`), live scene (`inspect_godot_editor`), and AuraPreview (`inspect_godot_asset_preview`) inspection for an explicit visual checkpoint or an unresolved concrete diagnostic.
- Produces one compact Worker item for the current live procedural construction request, preserving the user’s semantic intent and no-save instruction.
- Names `build_live_ruin` as the primary mutation tool.
- Must not attempt mutations, read bridge credentials, prescribe raw resource paths, or author another execution path.

##### Worker (owns every mutation)

- Uses `inspect_live_ruin_contract` at most once when needed, then immediately uses `build_live_ruin` with semantic parameters and stable handles.
- For a large place, completes all requested connected zone batches inside the same Worker item and uses each successful call's returned handles and spaces in the next call.
- Uses `edit_godot_asset_preview` only as a narrow fallback for a catalog operation the project vocabulary does not support; it is not the normal structural path.
- Keeps every generated piece beneath the genuine `AuraPreview` and never saves unless explicitly requested.
- On a rejected procedural batch, use its structured diagnostic to fix only that zone or report the concrete semantic/geometry error. Do not nudge individual mesh transforms.

#### Visual Checkpoints and Styling Work

- Use visual evidence when the user explicitly asks to take a look, inspect the composition, or judge how it reads, or when the task genuinely depends on visual judgment: styling, silhouette, imposing character, believable damage, or autonomous broad design and refinement.
- Inspect exact scene facts, capture a useful view with `capture_godot_asset_preview`, and call `critique_godot_preview_local` when it is installed and callable. Make focused revisions when requested.
- Never claim visual coherence or quality without useful visual evidence. Capture and critique are task-driven checkpoints, not mandatory proof after each construction mutation.

#### Safety and Scope

- Use catalog-only asset IDs and project calibrations; no arbitrary `.tscn` paths.
- No raw TCP, bridge tokens, second bridge, helper generator authored during a live composition, autonomous builder state machine, grammar engine, WFC, critic, or scoring system.
- Invalid geometry must reject before mutation; never accept a half-applied structure.
- Keep every `build_live_ruin` call atomic. A failed zone applies nothing from that call; successful earlier zone calls remain in the unsaved workshop.
- Do not use sleeps, timers, staged playback, or one model call per mesh piece. Progressive construction is model-observed semantic iteration, not animation of a precomputed batch.
- Never save the scene unless explicitly requested.
