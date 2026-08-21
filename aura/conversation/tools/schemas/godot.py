"""Godot-specific inspection and editing tool schemas."""
from __future__ import annotations

from typing import Any

GODOT_READ_TOOL_DEFS: list[dict[str, Any]] = [
    {
            "type": "function",
            "function": {
                "name": "inspect_godot_assets",
                "description": (
                    "Inspect recognized, project-specific Godot asset catalogs without changing files or the open "
                    "scene. Returns generic asset descriptors with resource paths, domains, kinds, tags, semantic "
                    "roles, dimensions, sockets, placement modes, calibrations, and catalog diagnostics. Covers the "
                    "project's ruin, camp, barrier, building, and prop kits. Catalog adapters remain separate from "
                    "Aura's conversation loop."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional exact domain filters, such as ['ruins'] or ['camps'].",
                        },
                        "kinds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional exact asset-kind filters, such as ['wall_corner'].",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Require every listed project tag.",
                        },
                        "semantic_roles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Require every listed generic role, such as entrance, barrier, or cover.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional case-insensitive text search across identity and semantics.",
                        },
                        "max_items": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                            "default": 50,
                            "description": "Maximum matching asset records to return. Summaries still cover all assets.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "inspect_godot_asset_preview",
                "description": (
                    "Inspect AuraPreview in the scene currently open in Godot. Returns catalog identities, "
                    "transforms, semantic roles, and conservative footprint-overlap diagnostics. This is read-only "
                    "and reflects the live scene, including changes made by edit_godot_asset_preview."
                ),
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
    {
            "type": "function",
            "function": {
                "name": "capture_godot_asset_preview",
                "description": (
                    "Capture a viewport rendering of the current Godot AuraPreview scene and return capture "
                    "metadata and live preview facts. Parameters are optional. Returns capture_set_id, "
                    "scene_path, scene_fingerprint, preview facts from preview.snapshot, structural validation, "
                    "and per-capture entries with view name, workspace-relative path, dimensions, and sha256 "
                    "digest. No image bytes or base64 data appear in the result. "
                    "Read-only — no approval needed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "capture_set_id": {
                            "type": "string",
                            "description": "Stable label for this capture set. Must not contain '..', '/', or '\\'. Default: auto-generated timestamp.",
                        },
                        "width": {
                            "type": "integer",
                            "minimum": 64,
                            "maximum": 1920,
                            "default": 1280,
                            "description": "Viewport width in pixels.",
                        },
                        "height": {
                            "type": "integer",
                            "minimum": 64,
                            "maximum": 1080,
                            "default": 720,
                            "description": "Viewport height in pixels.",
                        },
                        "modes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 4,
                            "description": "One or more of 'current_editor', 'overview', 'top_down'. Default: ['current_editor'].",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "inspect_godot_api",
                "description": (
                    "Query the exact engine API exposed by the live Godot editor's ClassDB. Search engine class "
                    "names or inspect a class's methods, argument/default signatures, properties, signals, integer "
                    "constants, and enums. This is read-only and version-exact. ClassDB does not include project "
                    "script-defined class_name types."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "class_name": {
                            "type": "string",
                            "description": "Exact engine class to inspect. Omit to search class names.",
                        },
                        "member_query": {
                            "type": "string",
                            "description": "Case-insensitive class-name or member-name filter.",
                        },
                        "include_inherited": {
                            "type": "boolean",
                            "default": False,
                            "description": "Include inherited members when inspecting one class.",
                        },
                        "max_items": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                            "default": 50,
                            "description": "Maximum returned matches in each bounded member section.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "inspect_godot_editor",
                "description": (
                    "Inspect the scene currently open in the live Godot editor. Returns the exact scene tree, "
                    "selected nodes, node types, scripts, transforms, and editor-visible properties. This is "
                    "read-only. Requires the Aura Editor Bridge to be installed (install_godot_editor_bridge) and "
                    "Godot to have the project open; otherwise the call fails with that reason."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_properties": {
                            "type": "boolean",
                            "description": "Include serialized Inspector properties for every node.",
                            "default": True,
                        },
                        "max_nodes": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 2000,
                            "description": "Maximum number of scene nodes to return. Default: 500.",
                            "default": 500,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }
]

GODOT_WRITE_TOOL_DEFS: list[dict[str, Any]] = [
    {
            "type": "function",
            "function": {
                "name": "install_godot_editor_bridge",
                "description": (
                    "Install and enable Aura's modular, localhost-only EditorPlugin in the current Godot project. "
                    "This creates addons/aura_bridge and writes a token-authenticated local config. By default, "
                    "activate it normally in Godot under Project Settings > Plugins. Set enable_plugin only for "
                    "headless setup. This is the bundled bridge implementation; it does not author project code."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "port": {
                            "type": "integer",
                            "minimum": 1024,
                            "maximum": 65535,
                            "default": 17891,
                        },
                        "enable_plugin": {
                            "type": "boolean",
                            "description": (
                                "Write the enabled plugin entry into project.godot. Defaults to false so the user "
                                "can activate it normally through Godot's Plugins UI."
                            ),
                            "default": False,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "edit_godot_editor",
                "description": (
                    "Manipulate the scene currently open in the live Godot editor. Changes use Godot's own "
                    "EditorUndoRedoManager. Use action=apply for node/property operations, action=select to focus "
                    "nodes in Godot, or action=save to save the active scene. "
                    "Values use Godot Variant text such as 'Vector3(1, 2, 3)', 'true', or '\"hello\"'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["apply", "select", "save"]},
                        "label": {"type": "string", "description": "Undo-history label for apply."},
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Root-relative node paths for select; '.' is the scene root.",
                        },
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string", "enum": ["set_property", "create_node"]},
                                    "node_path": {"type": "string"},
                                    "property": {"type": "string"},
                                    "value_text": {"type": "string"},
                                    "parent": {"type": "string", "default": "."},
                                    "name": {"type": "string"},
                                    "type": {"type": "string"},
                                    "properties": {
                                        "type": "object",
                                        "additionalProperties": {"type": "string"},
                                    },
                                },
                                "required": ["action"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "edit_godot_asset_preview",
                "description": (
                    "Safely assemble catalog-approved PackedScenes beneath a dedicated AuraPreview Node3D in the "
                    "scene currently open in Godot, or clear its children. Every call is approval-gated and one "
                    "Godot UndoRedo action. Asset IDs must come from inspect_godot_assets; arbitrary resource paths "
                    "are not accepted. This never saves the scene automatically."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["instantiate", "clear", "apply"]},
                        "label": {"type": "string", "description": "Undo-history label."},
                        "placements": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 64,
                            "description": "Required for instantiate and ignored for clear.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "asset_id": {"type": "string"},
                                    "domain": {"type": "string", "description": "Required only if the ID is ambiguous."},
                                    "name": {"type": "string", "description": "Optional unique node name."},
                                    "position": {
                                        "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3
                                    },
                                    "rotation_degrees_y": {"type": "number", "default": 0},
                                    "scale": {
                                        "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3
                                    },
                                },
                                "required": ["asset_id"],
                                "additionalProperties": False,
                            },
                        },
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 25,
                            "description": "Atomic ordered revision operations required for action=apply.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "operation": {
                                        "type": "string",
                                        "enum": ["set_transform", "instantiate", "remove", "replace"],
                                    },
                                    "node_path": {
                                        "type": "string",
                                        "description": "Direct child path such as AuraPreview/WestWall.",
                                    },
                                    "asset_id": {"type": "string"},
                                    "domain": {"type": "string"},
                                    "name": {"type": "string"},
                                    "position": {
                                        "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3
                                    },
                                    "rotation_degrees_y": {"type": "number"},
                                    "scale": {
                                        "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3
                                    },
                                },
                                "required": ["operation"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "edit_godot_scene",
                "description": (
                    "Apply structured node edits to an existing Godot .tscn text scene: add or remove nodes and "
                    "change node properties, without hand-editing the scene's text. "
                    "Node paths are relative to the scene root: '.' is the root and 'Player/Sprite' is a descendant. "
                    "Property values are raw one-line Godot expressions such as 'Vector2(10, 20)', 'true', "
                    "'\"Ready\"', or 'ExtResource(\"1_script\")'. All operations are validated in memory and "
                    "presented together in one approval diff before the scene is written."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path to an existing .tscn scene.",
                        },
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": [
                                            "add_node",
                                            "remove_node",
                                            "set_property",
                                            "remove_property",
                                        ],
                                    },
                                    "node_path": {
                                        "type": "string",
                                        "description": "Existing node path for remove/property operations.",
                                    },
                                    "name": {
                                        "type": "string",
                                        "description": "New node name for add_node.",
                                    },
                                    "type": {
                                        "type": "string",
                                        "description": "Godot class name for add_node, e.g. CharacterBody2D.",
                                    },
                                    "parent": {
                                        "type": "string",
                                        "description": "Parent node path for add_node; defaults to '.'.",
                                        "default": ".",
                                    },
                                    "property": {
                                        "type": "string",
                                        "description": "Godot property name for property operations.",
                                    },
                                    "value": {
                                        "type": "string",
                                        "description": "Raw one-line Godot value expression for set_property.",
                                    },
                                    "properties": {
                                        "type": "object",
                                        "description": "Optional property-to-raw-expression map for add_node.",
                                        "additionalProperties": {"type": "string"},
                                    },
                                    "recursive": {
                                        "type": "boolean",
                                        "description": "Required to remove a node that has descendants.",
                                        "default": False,
                                    },
                                },
                                "required": ["action"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["path", "operations"],
                    "additionalProperties": False,
                },
            },
        }
]
