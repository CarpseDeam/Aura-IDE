---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**", "scripts/live/**", ".aura/tools/**"]
triggers: ["aura preview", "godot bridge", "build_live_ruin", "procedural construction", "assemble", "godot live", "live scene", "live composition"]
---
### Godot Live Building — Natural Language Architectural Programs

Build the place the user described without making them learn procedural vocabulary. `build_live_ruin` is the primary mutation tool. Translate ordinary language into the project-owned architectural program silently; never ask the user for operation names, facade terminals, prompt keywords, asset IDs, or other "magic words."

For a new structure:

`inspect compact contract → inspect build schema if needed → build one connected architectural program → read compact receipt → revise that blueprint`

For a bounded follow-up:

`inspect existing blueprint if needed → revise the named mass/profile → preserve unaffected geometry → short factual receipt`

#### Interactive Mode

- Use one Interactive Mode for live building. Small edits and progressive builds are workflows selected by instruction scope, not separate execution modes.
- Read the user's requested place literally, including mood and ruin history. Infer a clear approach axis, dominant mass, subordinate masses, silhouette, facade rhythm, circulation, roof/crown, and coherent damage story.
- For any new building larger than a single wall edit, use `build_architectural_program`. One program may contain several connected masses; do not assemble a monastery, tower, gatehouse, or fortress from compatibility enclosures and attached rooms.
- Start with `inspect_live_ruin_contract(detail="summary")`. If exact nested fields are unknown, request only `detail="operation", operation="build_architectural_program"`; do not load the compatibility catalog.
- Put exactly one cohesive semantic operation in each `build_live_ruin` call. The model must receive that call's completed post-apply state before choosing the next operation; do not submit future live-build calls beside it.
- Continue beneath the existing real `AuraPreview`. Later requests modify the same `blueprint_id` with stable mass handles and preserve successful unaffected masses.
- Use returned blueprint identity, mass handles, mass deltas, entrance facts, piece counts, and diagnostics as facts. A compact receipt is intentionally not a mesh or graph dump.
- Do not treat returned metadata, operation names, or successful validation as proof that the requested architecture is complete or visually successful.
- Do not inspect constructor source, catalog files, exact node transforms, or implementation details during ordinary semantic construction when the contract supplies the needed syntax and references. Never create probe geometry to learn behavior.
- Never save the scene unless explicitly requested. Each procedural call is one atomic Godot `UndoRedo` action.

#### Architectural Composition

- Make one mass visually dominant. Vary subordinate height, footprint, setback, crown, or ruin state so the result does not read as equal boxes.
- Use stepped or orthogonal footprints where the concept needs an apse, offset wing, irregular tower, or changing silhouette. Rectangles are valid components, not the whole architectural language.
- Give long facades depth through buttressed bays, arcades, recesses, readable entrances, or attached masses. Keep windows subordinate and avoid uninterrupted slabs.
- Frame the principal entrance and keep an approach axis readable. Paired towers should differ in at least one of height, setback, crown, or damage.
- Concentrate ruin into one or two cause-and-effect regions: a fallen upper corner exposing a floor, a missing roof zone, a breached wall, or a surviving shell. Avoid uniform random deletion.
- Prefer role-aware facade defaults. Author a facade grammar only for a defining rhythm such as a processional arcade or monumental recessed gate.
- If a call fails, use its exact structured diagnostic and valid corrective candidates to fix only that operation. Geometry and topology failures do not require vision or individual mesh nudges.
- If an API interruption follows a successful step, inspect current state, continue from the returned blueprint and mass handles, and never rebuild an already-present blueprint.

#### Architectural Operations

- `build_architectural_program` creates the connected mass composition and all project-owned realization beneath it.
- `inspect_architectural_program` returns the durable blueprint facts needed for a follow-up.
- `revise_architectural_mass`, `revise_facade_grammar`, `revise_vertical_profile`, `revise_roof`, `revise_ruin_profile`, and `revise_circulation` change one architectural concern while preserving unrelated masses.
- `apply_architectural_dressing` is a finishing revision, not a substitute for silhouette or facade depth.
- `export_architectural_blueprint` exports only when requested.
- Older run, enclosure, room, wall-course, exact-piece, and surface operations are compatibility controls for explicitly requested maintenance of an existing legacy construction. Never choose them for a new broad architectural request.

#### Planner and Worker Role Split

##### Planner (read-only)

- Preserve the user's literal request and no-save instruction in one compact Worker item.
- Name `build_live_ruin` as the primary mutation tool.
- Use `inspect_godot_assets`, `inspect_godot_editor`, or `inspect_godot_asset_preview` only when their facts are actually needed. Do not attempt mutations, read bridge credentials, prescribe raw resource paths, or author another execution path.

##### Worker (owns every mutation)

- Use the focused contract inspection, then `build_live_ruin` with one architectural-program operation and stable blueprint/mass handles.
- Use `edit_godot_asset_preview` only as the narrow fallback described above.
- Keep every generated piece beneath the genuine `AuraPreview`, every call atomic, and the scene unsaved unless the user explicitly permits saving.

#### Visual Checkpoints

- Do not automatically call `capture_godot_asset_preview` or `critique_godot_preview_local`, start an unsolicited refinement loop, or capture merely to prove a semantic edit succeeded.
- When the user asks for visual judgment or a genuinely visual question remains, inspect exact scene facts, capture a useful view with `capture_godot_asset_preview`, and use a vision-capable tool such as `critique_godot_preview_local` when available.
- Capturing an image alone is not visual analysis. Never claim visual coherence or quality without visual findings, and do not make vision a mandatory gate between semantic construction steps.

#### Safety and Scope

- Use catalog-only asset IDs and project calibration; no arbitrary `.tscn` paths or raw transforms.
- No raw TCP, bridge tokens, second bridge, helper generator, autonomous builder state machine, named building generators, or mandatory vision.
- Invalid geometry must reject before mutation. A failed call applies nothing from that call; successful earlier calls remain in the unsaved preview.
- Never save the scene unless explicitly requested.
