# Godot asset visual iteration roadmap

This plan grows Aura from semantic scene awareness into a bounded assemble → inspect → revise loop
without coupling the conversation system to one game, asset kit, or oversized bridge script.

## Safety invariants

- The Godot integration remains optional and editor-only.
- Read operations and write operations use separate tools and handlers.
- Live changes require approval, use `EditorUndoRedoManager`, and never save automatically.
- Assembly is contained beneath a marked `AuraPreview` node in the open 3D scene.
- Aura accepts catalog identities, not arbitrary resource paths, for asset instancing.
- Project-specific semantics live behind catalog adapters; the core model stays generic.
- Requests are loopback-only, token-authenticated, bounded, and explicitly routed.
- Missing catalogs, bridge downtime, non-3D scenes, or unsupported assets fail without changing the scene.

## Phase 0 — live semantic bridge (complete)

- Inspect the open scene tree, selection, transforms, scripts, and editor properties.
- Select nodes, create basic nodes, set properties, and save only when explicitly asked.
- Keep transport, routing, perception, and actions in separate Godot scripts.

## Phase 1 — generic asset intelligence (complete)

- Describe assets through generic identities, roles, dimensions, sockets, and placement modes.
- Discover project-specific catalogs through pluggable adapters.
- Query catalogs with `inspect_godot_assets` without changing files or scenes.
- Use the V_Ruins/RuinLab JSON catalog as the first adapter, not as core architecture.

## Phase 2 — safe asset instancing (complete: first bounded slice)

- `edit_godot_asset_preview` resolves requested IDs through a recognized catalog.
- The bridge instantiates only resolved `res://` `PackedScene` resources with `Node3D` roots.
- Placements are bounded to 64 per action, ±10 km, and scale 0.01–100.
- Instances live directly beneath a marked `AuraPreview` root.
- Instantiate and clear are atomic Godot undo actions; neither saves the scene.
- Existing nodes outside `AuraPreview` cannot be removed or reparented through this tool.

Next hardening: add socket-aware placement and explicit promotion from disposable preview to a
user-chosen production parent.

## Phase 3 — structural validation (complete: first conservative slice)

- `inspect_godot_asset_preview` reads the live preview and maps instances back to catalog semantics.
- It reports transforms, kinds, domains, roles, unrecognized children, and elevated ground assets.
- It flags likely intersections using rotated catalog footprint approximations.
- Diagnostics state that footprint overlap is an approximation, not visual truth.

Next hardening: socket alignment, terrain contact, navigation clearance, enclosure/connectivity checks,
and adapter-provided domain rules for camps, barriers, buildings, vegetation, and other MMO kits.

## Phase 4 — controlled preview capture (next)

- Capture a chosen Godot 3D editor viewport through `EditorInterface.get_editor_viewport_3d()`.
- Store captures in a bounded, disposable location with an explicit cleanup policy.
- Carry camera transform, viewport index, scene revision, and preview snapshot alongside the image.
- Deliver the image through Aura's actual multimodal input path; a PNG path in text is insufficient.

## Phase 5 — visual critic

- Compare the controlled capture with the semantic snapshot.
- Produce observations such as cramped spacing, weak silhouette, dominance, repetition, or poor framing.
- Separate observations from proposed edits and attach confidence/evidence.
- Keep semantic geometry authoritative for identity, placement, and collision decisions.

## Phase 6 — bounded assemble → look → revise

- Translate a user intent into a small placement plan.
- Present one approval batch, instantiate it, inspect structure, and capture a preview.
- Propose a bounded revision batch; do not silently enter an unbounded autonomous loop.
- Preserve an audit record and a reliable rollback point for every iteration.

## Phase 7 — project adapters and UX

- Add adapters for camps, barriers, buildings, props, foliage, roads, and gameplay markers as metadata
  becomes available.
- Add reusable layout recipes without baking MMO-specific concepts into the bridge.
- Surface preview state, diagnostics, image comparisons, undo, clear, and promote controls in Aura.

## Current first-use loop

1. Open the intended 3D preview scene in Godot and enable the Aura Editor Bridge.
2. Ask Aura to inspect available assets with `inspect_godot_assets`.
3. Ask a Worker to place a small named set with `edit_godot_asset_preview`.
4. Approve the preview batch.
5. Ask Aura to run `inspect_godot_asset_preview` and explain any diagnostics.
6. Revise with another approved placement batch, use Godot Undo, or clear `AuraPreview`.
7. Save only when the result is intentionally meant to persist.
