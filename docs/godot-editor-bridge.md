# Live Godot editor bridge

Aura's live bridge complements `.tscn` file editing with access to the scene that is actually open in
Godot. It is an editor-only addon; it is not added to exported games.

## Setup

Open the Godot project as Aura's workspace and ask Aura to install the editor bridge. Aura calls
`install_godot_editor_bridge`, which:

- copies the bundled addon to `addons/aura_bridge/`;
- creates `.aura/godot_editor_bridge.json` with a random authentication token;
- leaves activation to Godot's normal **Project Settings → Plugins** UI by default.

Enable **Aura Editor Bridge** in Godot afterward. The bridge listens only on
`127.0.0.1` and accepts only its small, whitelisted protocol.

## Iteration loop

1. `inspect_godot_editor` reads the open scene tree, selection, node types, scripts, transforms, and
   editor-visible properties.
2. `inspect_godot_api` queries the running editor's exact ClassDB classes, methods, defaults,
   properties, signals, constants, and enums before Aura relies on an uncertain Godot API.
3. `edit_godot_editor` selects nodes or applies approved `create_node` and `set_property` operations
   through Godot's `EditorUndoRedoManager`.
4. `edit_godot_asset_preview` instantiates catalog-approved 3D scenes beneath the disposable,
   marked `AuraPreview` root, clears it, or applies one atomic batch of transform, instantiate,
   remove, and replace operations.
5. `inspect_godot_asset_preview` maps the live instances back to catalog semantics and reports
   conservative footprint-overlap diagnostics.
6. `capture_godot_asset_preview` validates a live editor viewport capture and returns bounded local
   structural evidence without putting image bytes into conversation history.
7. A personal, unpackaged `.aura/tools` integration may optionally ask loopback Ollama for aesthetic
   observations; the existing Worker remains the decision owner.
8. `edit_godot_editor` with `action: "save"` saves the active scene only when explicitly requested.
9. Aura inspects again and compares the resulting live state.

Node paths are relative to the edited scene root (`.` means the root). Property values use Godot's
Variant text format, such as `Vector3(1, 2, 3)`, `70.0`, `true`, or `"Ready"`.

## Knowledge layer

`inspect_godot_api` provides exact ClassDB reflection from the running editor. If the bridge is
offline, it runs the same bounded query through Aura's configured Godot executable. It reports engine
method signatures/defaults, properties/defaults/getters/setters, signals, enums, constants,
inheritance, and project `class_name` metadata. Script-defined method bodies still come from project
code inspection.

Personal project skills in `.aura/skills/authored/` add selectively loaded engineering judgment. The
reference pack in `scripts/personal/godot_knowledge/` covers:

- scene architecture, signals, groups, Resources, and restrained autoload use;
- Godot 4.6 GDScript lifecycle and validation practice;
- Node3D transforms, PackedScene ownership, modular sockets, and preview assembly;
- MMO/large-world rendering, physics, navigation, streaming, and authority tradeoffs;
- Aura's inspect → assemble → validate → capture → critique → revise workflow.

These skills are outside the packaged `aura` tree and are copied into a workspace only when wanted.
They complement exact API reflection instead of trying to memorize every engine signature.

## Boundaries

The current bridge provides semantic perception, undoable creation/property edits, bounded asset
preview assembly/revision, and controlled 3D viewport capture. General scene removal, reparenting,
runtime-game inspection, and preview promotion remain outside this catalog-safe surface.

The staged safety and implementation plan for reusable asset assembly is documented in
[`aura-godot-visual-iteration-corrected-plan.md`](../tmp/aura-godot-visual-iteration-corrected-plan.md).
