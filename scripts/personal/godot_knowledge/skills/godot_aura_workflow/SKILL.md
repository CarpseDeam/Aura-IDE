---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**"]
triggers: ["aura preview", "godot bridge", "inspect_godot_api", "inspect_godot_assets", "capture_godot", "visual iteration", "assemble", "edit_godot_asset_preview", "godot live", "live scene", "live composition"]
---
### Godot Live Building — Fast Interactive Loop

The intended same-turn loop is:

`inspect once → build a connected burst → capture → describe locally → DeepSeek reacts → build the next burst`

#### Interactive Mode

- DeepSeek is the builder and sole decision-maker.
- Inspect the asset catalog and current preview at the beginning, then reuse known asset IDs, sockets, and named `AuraPreview` paths.
- Build several meaningful connected pieces per atomic `edit_godot_asset_preview` apply call. Think like building a Minecraft structure: establish a wall run, turn a corner, create an opening, extend a room, add a tower mass, then continue.
- After a meaningful structural burst, call `capture_godot_asset_preview`, then pass the returned capture path to `describe_godot_preview_local`.
- DeepSeek consumes that description and immediately continues the current request in the same tool loop.
- Do not stop after each burst to wait for another user message.
- Do not call vision after every individual wall, prop, or rubble piece.
- Do not repeatedly inspect the full catalog.
- Do not require a verdict, coherence proof, checklist, score, critic approval, or mandatory revision ritual.
- Do not clear and rebuild when named duplicates, attachments, branches, replacements, removals, or transforms can extend the existing result.
- Continue until the user's current requested construction or revision is actually developed, the user cancels or redirects, or a real tool failure prevents further progress.
- Later user instructions such as `add a tower`, `make it more run down`, or `add another room` should modify the existing live `AuraPreview`, not begin a new autonomous design system.
- Never save the scene unless explicitly requested.
- Preserve catalog-only asset IDs, the genuine `AuraPreview` root, atomic UndoRedo-backed bursts, no raw TCP, no bridge credentials, no helper builders or generators, and no arbitrary `.tscn` paths.

#### Planner and Worker Role Split

##### Planner (read-only)
- May inspect project conventions, catalog (`inspect_godot_assets`), live scene (`inspect_godot_editor`), AuraPreview (`inspect_godot_asset_preview`), uncertain Godot APIs (`inspect_godot_api`), and captured visual evidence (`capture_godot_asset_preview`).
- Produces one compact Worker item for the complete live-editor composition request.
- Preserves the user's original visual intent, constraints, and no-save instruction.
- Names the existing conversation tools the Worker should use, including `describe_godot_preview_local` when local vision is available.
- Must NOT write helper scripts, source files, builders, generators, resources, tests, or documentation for a live composition request.
- Must NOT read bridge credentials, author TCP clients, call bridge protocol actions directly, or invent another execution path.
- Must NOT prescribe raw resource paths or bypass catalog asset IDs.
- Must NOT attempt mutations itself.

##### Worker (owns every mutation and iteration step)
- Uses `inspect_godot_assets`, `inspect_godot_editor`, `inspect_godot_asset_preview`, `edit_godot_asset_preview`, `capture_godot_asset_preview`, `describe_godot_preview_local` (when available), and `edit_godot_editor`.
- Uses catalog asset IDs through `edit_godot_asset_preview`, never raw `.tscn` paths or direct TCP.
- Keeps all composition nodes beneath the genuine `AuraPreview` root.
- Builds several meaningful connected pieces in one atomic apply call per burst.
- After a structural burst, captures and describes locally, then immediately continues building in the same request.
- Never saves the scene unless explicitly requested.

#### Building Cadence
- Establish footprint, major structural runs, entrances, and primary landmarks in the first few bursts.
- Connect corners, complete spatial relationships, and add secondary structures.
- Add breaches, damage, rubble, props, and visual storytelling.
- Capture and describe after meaningful structural bursts, not after every individual piece.
- DeepSeek consumes the local description and continues building — no verdict, no approval gate, no mandatory revision pass.
- When the local description tool is unavailable, continue building from structural facts and preview inspection; do not claim visual coherence without visual evidence.

#### Forbidden Actions
- No raw TCP or direct bridge-protocol calls from Planner or Worker prompts.
- No bridge-token or credential access in skill text.
- No helper scripts, builders, generators, resources, test files, or documentation produced for live composition.
- No arbitrary `.tscn` paths — all assets must come from the catalog via `inspect_godot_assets`.
- No scene saving unless explicitly requested.
- No Planner-side mutations of the Godot editor.
- No verdict, scoring, coherence checklist, mandatory critique, or forced revision-loop ritual.
