"""Tool definition schemas for Drone-related tools."""

from __future__ import annotations

from typing import Any

LAUNCH_READ_ONLY_DRONE_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "launch_read_only_drone",
        "description": (
            "Launch a saved read-only Drone in the background for a focused "
            "investigation sub-task. Returns immediately with a run_id. "
            "Use check_drone_run later to retrieve results. "
            "Use this when the task is a focused side investigation (bug tracing, "
            "impact scouting, test discovery) that would otherwise burn tool calls "
            "or clutter the main conversation. Do NOT use for tiny tasks where "
            "direct inspection is faster."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "drone_id": {
                    "type": "string",
                    "description": "The id of the saved read-only Drone to run (from Available Drones list).",
                },
                "goal": {
                    "type": "string",
                    "description": "What the Drone should investigate or accomplish. Be specific so the Drone's instructions can guide it precisely.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional: why you are launching this Drone. Used only for logging.",
                },
            },
            "required": ["drone_id", "goal"],
        },
    },
}


RUN_READ_ONLY_DRONE_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_read_only_drone",
        "description": (
            "Run a saved read-only Drone directly in the background to handle a "
            "focused sub-task. Returns results synchronously. For current-info "
            "questions that need fresh web evidence, call this with "
            "drone_id='web-research' and pass the user's question as goal text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "drone_id": {
                    "type": "string",
                    "description": "The id of the saved read-only Drone to run (from Available Drones list).",
                },
                "goal": {
                    "type": "string",
                    "description": "What the Drone should investigate or accomplish. Must be non-empty.",
                },
                "ui_mode": {
                    "type": "string",
                    "enum": ["silent", "visible"],
                    "description": (
                        "For drone_id='web-research' only. Use 'silent' for "
                        "answer-only research so no browser, report, Workbay, "
                        "Terminal, or other work surface is shown. Use "
                        "'visible' only when the user explicitly asks for a "
                        "visible browser or research UI."
                    ),
                },
            },
            "required": ["drone_id", "goal"],
        },
    },
}


CHECK_DRONE_RUN_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "check_drone_run",
        "description": (
            "Check the status of a previously launched read-only Drone run. "
            "Returns queued/running/completed/failed/timed_out state. "
            "If completed, includes summary, tool call counts, and elapsed time. "
            "Optionally wait a few seconds for completion (capped at 10s)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run_id returned from launch_read_only_drone.",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Optional: seconds to wait for completion (capped at 10). Default 0 (return immediately).",
                },
                "include_receipt": {
                    "type": "boolean",
                    "description": "If true, include the full receipt in the result. Default false.",
                },
            },
            "required": ["run_id"],
        },
    },
}


REGISTER_DRONE_FOLDER_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "register_drone_folder",
        "description": (
            "Validate and register a completed folder-backed Drone. "
            "The folder must already contain drone.json and an entrypoint program. "
            "The manifest must declare a command entrypoint with json-stdio protocol. "
            "Registration validates the folder structure and copies it into "
            "Aura's global Drone directory. Real Drone behavior is checked "
            "when the user runs the Drone from Workbay."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folder_path": {
                    "type": "string",
                    "description": (
                        "Workspace-relative path to the completed Drone folder, "
                        "for example .aura/drone-build/source-scout."
                    ),
                },
            },
            "required": ["folder_path"],
        },
    },
}


DECLARE_UI_CONTRACT_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "declare_ui_contract",
        "description": (
            "Declares the UI contract the launch gate verifies after boot. "
            "Must be called before the worker lap edits code. Each assertion "
            "names a node the post-edit accessibility tree must contain or "
            "must not contain."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folder_path": {
                    "type": "string",
                    "description": "The drone folder path, same convention register_drone_folder uses.",
                },
                "assertions": {
                    "type": "array",
                    "description": "List of UI assertions to verify after boot.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["node_exists", "node_absent"],
                                "description": "Whether the node must exist or be absent.",
                            },
                            "role": {
                                "type": "string",
                                "description": "Accessibility role of the node.",
                            },
                            "name": {
                                "type": "string",
                                "description": "Accessibility name of the node.",
                            },
                            "object_name": {
                                "type": "string",
                                "description": "Qt object name of the node.",
                            },
                        },
                    },
                },
            },
            "required": ["folder_path", "assertions"],
        },
    },
}
