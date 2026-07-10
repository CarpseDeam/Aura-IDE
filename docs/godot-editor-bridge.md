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
3. `edit_godot_editor` with `action: "save"` saves the active scene.
4. Aura inspects again and compares the resulting live state.

Node paths are relative to the edited scene root (`.` means the root). Property values use Godot's
Variant text format, such as `Vector3(1, 2, 3)`, `70.0`, `true`, or `"Ready"`.

## Boundaries

The current first slice provides semantic perception and undoable creation/property edits. Removal,
reparenting, runtime-game inspection, and 3D viewport image capture belong in separate action and
perception modules so the bridge remains small and auditable.

The staged safety and implementation plan for reusable asset assembly is documented in
[`godot-asset-visual-iteration-roadmap.md`](../tmp/godot-asset-visual-iteration-roadmap.md).
