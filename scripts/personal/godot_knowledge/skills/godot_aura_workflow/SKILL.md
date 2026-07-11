---
task_kinds: ["visual iteration", "godot_bridge", "godot_assembly", "godot_visual_iteration"]
path_globs: ["addons/aura_bridge/**"]
triggers: ["aura preview", "godot bridge", "inspect_godot_api", "inspect_godot_assets", "capture_godot", "visual iteration", "assemble", "edit_godot_asset_preview", "godot live", "live scene", "live composition"]
---
### Godot Visual Iteration — Planner and Worker Role Split

#### Planner (read-only)
- May inspect project conventions, catalog (inspect_godot_assets), live scene (inspect_godot_editor), AuraPreview (inspect_godot_asset_preview), uncertain Godot APIs (inspect_godot_api), and captured visual evidence (capture_godot_asset_preview).
- Produces one compact Worker item for the complete live-editor composition request.
- Preserves the user's original visual intent, constraints, and no-save instruction.
- Names the existing conversation tools the Worker should use.
- Must NOT write helper scripts, source files, builders, generators, resources, tests, or documentation for a live composition request.
- Must NOT read bridge credentials, author TCP clients, call bridge protocol actions directly, or invent another execution path.
- Must NOT prescribe raw resource paths or bypass catalog asset IDs.
- Must NOT attempt mutations itself.

#### Worker (owns every mutation and iteration step)
- Uses inspect_godot_assets, inspect_godot_editor, inspect_godot_asset_preview, edit_godot_asset_preview, capture_godot_asset_preview, critique_godot_preview_local (when available), and edit_godot_editor.
- Uses catalog asset IDs through edit_godot_asset_preview, never raw .tscn paths or direct TCP.
- Keeps all composition nodes beneath the genuine AuraPreview root.
- Never saves the scene unless explicitly requested.

#### Layered Building Workflow
Build coherent environments in observable visible layers:
1. Establish footprint, major structural runs, entrances, and primary landmarks.
2. Connect corners and complete important spatial relationships.
3. Add secondary structures and interior organization.
4. Add breaches, damage, rubble, props, and visual storytelling.
5. Inspect exact live facts and structural diagnostics.
6. Capture overview visual evidence.
7. Obtain bounded local visual critique (optional, when the personal tool is installed).
8. Apply a focused atomic revision.
9. Reinspect, recapture, and continue while meaningful improvements remain.

Use structural facts (preview snapshot, footprint diagnostics) for exact geometry. Use image evidence (capture, critique) for composition, hierarchy, silhouette, readability, and atmosphere. Make each revision concrete and visible in the editor.

#### Pass Limit and Supervision
- When the user is actively supervising the run, there is no fixed revision-pass limit.
- Continue building and refining while each pass has a clear purpose and produces meaningful progress.
- Stop when the requested environment is coherent, the user cancels, no useful improvement remains, evidence is unchanged across repeated attempts, or a real tool failure prevents further work.
- Larger requests (castles, villages, forts, dungeon sections, marketplaces, camps, cities) should develop across many small observable passes rather than being forced into one giant placement or an arbitrary pass cap.

#### Composition Guidance
- Favor connected structural runs, deliberate corners, readable entrances, clear hierarchy, navigable interior space, and damage that follows from understandable structural breaks.
- Start with the largest structural elements, then secondary structures, then detail.
- Keep this approach generic to ruins, castles, villages, interiors, roads, camps, cities, fortifications, and other catalog-backed environments.
- Do not add project-specific scoring systems, hardcoded aesthetic validators, another autonomous loop, or hidden stopping machinery.

#### Forbidden Actions
- No raw TCP or direct bridge-protocol calls from Planner or Worker prompts.
- No bridge-token or credential access in skill text.
- No helper scripts, builders, generators, resources, test files, or documentation produced for live composition.
- No arbitrary .tscn paths — all assets must come from the catalog via inspect_godot_assets.
- No scene saving unless explicitly requested.
- No Planner-side mutations of the Godot editor.
