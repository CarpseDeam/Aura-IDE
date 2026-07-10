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
2. `edit_godot_editor` selects nodes or applies approved `create_node` and `set_property` operations
   through Godot's `EditorUndoRedoManager`.
3. `edit_godot_asset_preview` instantiates catalog-approved 3D scenes beneath the disposable,
   marked `AuraPreview` root or clears its children as one undoable action.
4. `inspect_godot_asset_preview` maps the live instances back to catalog semantics and reports
   conservative footprint-overlap diagnostics.
5. `edit_godot_editor` with `action: "save"` saves the active scene only when explicitly requested.
6. Aura inspects again and compares the resulting live state.

Node paths are relative to the edited scene root (`.` means the root). Property values use Godot's
Variant text format, such as `Vector3(1, 2, 3)`, `70.0`, `true`, or `"Ready"`.

## Boundaries

The current bridge provides semantic perception, undoable creation/property edits, and bounded asset
preview assembly. General removal, reparenting, runtime-game inspection, preview promotion, and 3D
viewport image capture remain separate milestones so the bridge stays small and auditable.

The staged safety and implementation plan for reusable asset assembly is documented in
[`godot-asset-visual-iteration-roadmap.md`](godot-asset-visual-iteration-roadmap.md).
