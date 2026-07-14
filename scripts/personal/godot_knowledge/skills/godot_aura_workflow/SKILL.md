---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**", "scripts/live/**", ".aura/tools/**"]
triggers: ["aura preview", "godot bridge", "build_live_ruin", "procedural construction", "assemble", "godot live", "live scene", "live composition"]
---
### Godot Live Building — Procedural Co-Building

For a small bounded edit, use the fast loop:

`one instruction → one build_live_ruin call → short receipt → wait`

Its supervision summary remains: `inspect once → dispatch one compact Worker item → semantic construction returns exact facts → report and wait for the next direction`.

For a broad creative place, use one progressive pass that completes one meaningful architectural component or visual checkpoint before another begins:

`inspect once if needed → meaningful mass → live state → height and silhouette → walls and openings → localized ruin character → visual checkpoint → user handoff`

#### Interactive Mode

- Use one Interactive Mode for live building. Small-edit and progressive large-build behavior are workflows selected by instruction scope, not separate execution modes.
- DeepSeek chooses structural intent; project code owns exact mesh positions, rotations, spacing, socket alignment, corner selection, opening widths, occupancy checks, and stable names.
- Classify the current instruction by scope. Keep one wall run, bounded room, floor region, level, upper fragment, stair, opening, connection, exact wall-piece placement, extension, or damage edit to one `build_live_ruin` call containing one cohesive semantic operation.
- Treat broad, multi-zone creative requests as progressive builds. Dispatch one compact Worker item for the requested pass, then make several result-driven `build_live_ruin` calls inside that same Worker item.
- Express footprint dimensions, cardinal directions, module counts, entrance sides, opening slots, attached sides, room dimensions, upper-level support, and selected damage sections. Never calculate a transform for every mesh.
- Build incrementally beneath the existing real `AuraPreview`. Later requests such as `extend the east room` or `breach the rear wall` modify the named live structure.
- Continue within the current request while another structural operation is needed to complete the current meaningful component. Do not pause after every wall or module.
- Never save the scene unless explicitly requested. Each procedural call is one atomic Godot UndoRedo action.

Use this action-first rhythm inside Interactive Mode:

- For live procedural construction, call `inspect_live_ruin_contract` at most once per request when the contract or current semantic state is unknown. Treat the contract and returned semantic state as authoritative.
- After the contract returns, make the first useful `build_live_ruin` call immediately. Do not inspect V_Ruins constructor source, catalog files, exact node transforms, or implementation details before that call or during ordinary semantic construction when the contract is available.
- Prefer a coarse but valid, meaningful architectural chunk that can be revised over prolonged preflight design intended to perfect the whole zone before applying anything. Use visible iteration as the planning mechanism: apply a meaningful chunk, observe its returned semantic result, then apply the next chunk.
- Emit exactly one `build_live_ruin` call in each assistant tool-call round and put exactly one cohesive semantic operation in that call. The model must receive the completed call's compact post-apply state before choosing or submitting the next construction step; do not place future live-build calls beside it in the same assistant message.
- After a successful build call, continue directly to the next `build_live_ruin` call from its returned mass map, vertical profiles, handles, spaces, connections, and diagnostics without another inspection. Read the factual mass map after every mass operation; do not inspect the preview to determine exact coordinates for the next semantic operation.
- For broad creative work, use returned structural facts to continue the current meaningful component. Unless the user explicitly requests blockout or foundation only, requested rooms and volumes do not by themselves prove the requested architecture is complete.
- For a broad architectural request, establish a hierarchy of structural masses before openings or detail: a primary low or wide mass, secondary taller or narrower masses, explicit height contrast and footprint transitions, immediately supported upper stages, required connectors or spans, then final narrower or partial silhouette fragments. These are compositional roles, not fixed dimensions, symmetry rules, or named templates.
- Complete structural massing and a readable silhouette before windows, exact wall pieces, arcades, colonnades, ceiling decoration, damage, vegetation, or debris. Use `add_ceiling_arches` only when the user explicitly requests a supported single-storey rib grid; never select it automatically after creating a large hall or treat it as a monumental nave ceiling.
- Stop after one meaningful component or useful visual checkpoint so the user can inspect and redirect before another component begins.
- Do not force symmetry, fixed proportions, or fixed motif sequences. Compose original results from verified walls, tall corners, windows, doors, pillars, arches, floors, stairs, broken variants, vegetation, and lighting without introducing named builders, templates, or new assets.
- Perform another read-only inspection only when a concrete structured diagnostic cannot be resolved from its returned valid candidates. Never create probe geometry to learn behavior.
- If an API interruption ends the turn after a successful step, reconstruct current state with `inspect_live_ruin_contract` and continue from returned handles. Never repeat an already-present stable handle or recreate geometry to catch up.

#### Rapid Supervised Construction

- This is the normal behavior while the user directs successive bounded edits such as adding or removing a room, level, stairs, opening, or connection; extending a hall; raising a wall or named space; or damaging a selected section.
- Dispatch one compact Worker item for the current instruction. Use the existing semantic project-local construction tool, such as `build_live_ruin`, when available.
- Trust the tool's returned snapshot reconstruction, validation, topology, stable handles, and atomic revision result as structural placement facts.
- Do not automatically call `capture_godot_asset_preview` or `critique_godot_preview_local`, start an unsolicited visual refinement loop, or capture merely to prove that a semantic edit succeeded.
- Return a short factual receipt of the structural result and wait for the user's next direction. A captured screenshot does not itself prove visual quality; claim visual verification only when a vision-capable tool returns visual findings.
- If the semantic operation fails, use its exact geometry or topology diagnostics first. Vision is not the default recovery path for structural failure.

#### Progressive Large Construction

- Inspect `inspect_live_ruin_contract` once when the semantic contract or current handles are unknown. Treat its operation schemas, grammar, live reconstruction, and valid candidates as authoritative.
- Do not inspect project source code to discover semantic operation syntax when the contract tool is available. Do not create disposable probe walls, rooms, or openings to infer coordinates, handles, anchors, attachment forms, or naming conventions.
- Break the requested component into meaningful connected semantic steps within the same Worker item, stopping when that component or a useful visual checkpoint is complete so the user can inspect and redirect.
- Do not force the entire place into one comprehensive `build_live_ruin` call. Apply each successful zone immediately with its own atomic `build_live_ruin` call so progress becomes visible.
- Read the compact post-apply mass map, ordered vertical profiles, handles, created or modified spaces, piece-count delta, openings, connections, continuation affordances, and validation diagnostics after each successful call and use them as references in the next step. Repeated-footprint counts and zero footprint deltas are factual warnings against blindly repeating the same stage. Do not guess a handle that the prior call did not return.
- Do not pause between the atomic calls needed to complete the current meaningful component. Return one concise factual receipt when that component reaches its checkpoint or a real semantic failure prevents continuation.
- Do not call `capture_godot_asset_preview`, `critique_godot_preview_local`, or any vision tool between semantic construction steps. Capture once the current building reaches a useful visual checkpoint, then return control so the user can direct the next addition.
- If one zone fails, keep prior successful zones, use the structured diagnostic to correct only the failed zone, and retry that zone. Never request partial application of a failed atomic call and do not add speculative retry machinery.

#### Procedural Vocabulary

- `create_run`: named straight run from a semantic start anchor, cardinal direction, module count, and catalog piece family.
- `turn_run`: correctly selected and oriented corner from a named run into a new cardinal direction.
- `extend_run`: add deterministic modules to an existing named run.
- `create_enclosure`: connected four-sided enclosure with footprint/module dimensions and a deliberate entrance.
- `insert_opening`: replace a named run slot with a doorway, breach, gap, damaged section, or intact wall.
- `attach_room` and `extend_room`: add or extend a named secondary enclosure from a named wall slot; use the child `room_slot` when an exact along-wall alignment or deliberate offset is required.
- `add_upper_wall_section` and `add_upper_level`: add supported wall courses, enclosed upper spaces, or floorless upper fragments using semantic footprints and levels.
- `add_supported_span`: create a real elevated rectangular structural space between two compatible same-level supports. Supply named supports, level, axis, cross-span depth, semantic offset, mode, perimeter sides, openings, and style; project code derives its centre, orientation, required longitudinal length, support contacts, collision, and reconstruction metadata.
- `place_wall_piece`: after structural massing exists, select one exact `asset_id` from `wall_placeable_assets` and target a named wall, explicit bay or bays, supported level, and interior or exterior face. Project calibration owns transforms, pivot correction, orientation, embed depth, projection, backing-wall relationship, and clearance.
- `apply_damage`: deterministically replace selected intact run slots with compatible damaged catalog variants.

#### Architectural Composition

- Translate requested architecture into a hierarchy of real connected spaces, wall courses, upper levels, supported spans, and floorless upper fragments before openings or surface detail.
- A broad request starts with structural mass roles: primary low or wide mass; secondary taller or narrower masses; explicit height contrast; footprint transitions; immediately supported upper stages; real connectors where required; narrower or partial crown fragments last.
- For a requested centre-and-flanks composition, verify facts from the returned mass map: the centre lies between the flank centres; flank top heights substantially exceed the centre; a requested upper connector lists and touches both supports; upper footprint dimensions or offsets change where required; and the adjacency/support graph connects every expected mass. Do not call the composition complete because three rooms exist.
- Do not encode fixed dimensions or mandatory symmetry. Use cardinal attachments, child wall slots, module offsets, support chains, footprint deltas, and span relationships to create either symmetric or intentionally asymmetric arrangements.
- When a requested connector is absent, flanks do not exceed the centre, masses are disconnected, the requested crown transition is absent, or vertical profiles repeat the same footprint several times, choose returned structural continuation candidates before windows or decorative candidates.
- Complete structural massing and silhouette before windows, arcades, colonnades, ceiling decoration, exact wall pieces, damage, vegetation, or debris.
- Never claim an architectural component exists merely because an operation has that name.
- Respect negative user constraints exactly. If the user says no towers or no roof yet, do not add them.
- Stop after one meaningful component or visual checkpoint so the user can inspect and redirect.
- Compose with general operations. Do not introduce named building templates, fixed archetypes, a new Planner path, or another execution mode.

#### Exact Wall-Piece Selection

- Inspect the real wall-placeable catalog and select exact assets by `asset_id`. Use factual descriptions, exact dimensions, attachment mode, intended visible face, calibrated penetration/projection, compatibility, material family, and tags.
- Tags may filter catalog results but never silently choose an asset. Do not translate an architectural idea or invented motif name into a hidden mesh choice.
- Structural operations such as `create_enclosure`, `attach_room`, `add_upper_level`, `add_supported_span`, `add_upper_wall_section`, `insert_opening`, `add_stair_run`, and `apply_damage` remain semantic because they describe real geometry.
- Never calculate or supply resource paths, world positions, rotations, scales, arbitrary offsets, or embed values for `place_wall_piece`.
- Read and report the returned exact piece facts: asset ID, wall, bay or bays, level, face, attachment mode, physical piece count, backing-wall result, calibrated embed, and validation.
- Successful validation proves safe, reconstructable placement. It does not prove coherent rhythm, hierarchy, visual quality, or any other composition judgment.

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
- Never claim visual coherence or quality without findings from a vision-capable tool. Capturing an image alone is not visual analysis. Capture and critique are task-driven checkpoints, not mandatory proof after each construction mutation; without visual analysis, report factual changes and let the user judge the preview.

#### Safety and Scope

- Use catalog-only asset IDs and project calibrations; no arbitrary `.tscn` paths.
- No raw TCP, bridge tokens, second bridge, helper generator authored during a live composition, autonomous builder state machine, grammar engine, WFC, critic, or scoring system.
- Invalid geometry must reject before mutation; never accept a half-applied structure.
- Keep every `build_live_ruin` call atomic. A failed zone applies nothing from that call; successful earlier zone calls remain in the unsaved workshop.
- Do not use sleeps, timers, staged playback, or one model call per mesh piece. Progressive construction is model-observed semantic iteration, not animation of a precomputed batch.
- Never save the scene unless explicitly requested.
