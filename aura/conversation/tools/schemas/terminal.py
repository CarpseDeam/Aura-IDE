"""Terminal and command-oriented tool schemas."""
from __future__ import annotations

from typing import Any

TERMINAL_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_terminal_command",
        "description": (
            "Execute one command in Aura's persistent PowerShell session in the workspace or an "
            "optional workspace-relative cwd and stream its output. The same PowerShell process "
            "is reused across commands and user turns in this conversation, so cwd, environment "
            "variables, and PowerShell variables persist. Each submitted command must still reach "
            "completion before the next one runs. The full system shell is available: pipes, redirects, and "
            "chaining work, as do linters, type checkers, test suites, build commands, git, "
            "dependency installs (pip install, python -m pip install, uv sync, poetry install, "
            "pdm install), and any command that mutates the workspace. The command runs with the "
            "workspace as its initial working directory unless cwd/working_directory is provided. Stdout "
            "and stderr are both captured and streamed in real-time. Returns the exit code and complete output on "
            "completion. The command must self-terminate: long-running watchers, dev servers, "
            "REPLs, and commands that wait for interactive input hit the timeout; the entire "
            "PowerShell session is then killed and the next command starts cleanly. "
            "For Python projects the project-local .venv interpreter is selected when present."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The PowerShell command to execute, e.g. 'python -m py_compile aura/app.py' for touched Python files, 'npm test' for a Node project when available/requested, or another focused validation/build command.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait before resetting the persistent session. Default follows Aura's terminal timeout policy; prefer short focused runs.",
                    "default": 300,
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional workspace-relative working directory for the command, e.g. 'companion-web'. Absolute paths and '..' escapes are rejected.",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Alias for cwd. Must be workspace-relative and stay inside the workspace.",
                },
            },
            "required": ["command"],
        },
    },
}

RUN_AND_WATCH_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_and_watch",
        "description": (
            "Run the task\'s declared run_command and watch for startup "
            "behavior. Success means the command exits on its own within the "
            "watch window with exit code 0 and no traceback. A command that "
            "survives the window without crashing (still running when the "
            "window expires) is FAILURE — the command must self-terminate. "
            "A crash (Traceback in output) or non-zero exit code is also "
            "failure. This tool takes NO command parameter — the command is "
            "fixed by the task contract (run_command "
            "field). If no run command was declared for this task, it "
            "returns an informational no-op result. Normally you do NOT "
            "need to call this tool yourself — the harness automatically "
            "runs launch verification after you finish. Use "
            "run_terminal_command for your own validation checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "window_seconds": {
                    "type": "integer",
                    "description": (
                        "How many seconds to watch the process. Default: 10. "
                        "Maximum: 60."
                    ),
                    "default": 10,
                },
            },
            "required": [],
        },
    },
}

DIAGNOSTIC_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_diagnostic_command",
        "description": (
            "Run ONE short, read-only, self-terminating command to inspect or validate the "
            "workspace, in the workspace root or an optional workspace-relative cwd. "
            "This is the default choice for checking your work: test runs, linters, type "
            "checks, git inspection (status, diff, log), and filesystem searches (rg, ls, cat). "
            "There is NO shell: the command is parsed into a single argument list and executed "
            "directly, so pipes, redirects, chaining (| & ; < > ` $(...)), installs, and any "
            "mutating command are rejected — use run_terminal_command for those. "
            "Quoting works normally and quoted arguments arrive unquoted, so pytest node IDs "
            "with spaces or parameters are safe to pass. For Python projects the project-local "
            ".venv interpreter is selected automatically from a bare 'python' or 'pytest'. "
            "Returns stdout, stderr, exit_code, timed_out, the requested command, and the argv "
            "that ran; a rejected command returns a failure_class, the offending token, and one "
            "concrete correction. Output is truncated at 100KB. "
            "Use this for a short read-only validation or inspection command."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "A read-only diagnostic command."                            "'python -m py_compile aura/gui/left_pane.py', "
                            "'git status', 'git diff', "
                            "'npm test', 'cargo test', "
                            "'rg \"class LeftPane\" aura/', "
                            "'ls aura/conversation/tools/'. "
                            "Use 'rg' instead of bare grep for shell searches, or use grep_search when you want structured matches. "
                            "For absence checks, make the command exit 0 when the pattern is absent."
                        ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait. Default: 30.",
                    "default": 30,
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional workspace-relative working directory for the diagnostic command. Absolute paths and '..' escapes are rejected.",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Alias for cwd. Must be workspace-relative and stay inside the workspace.",
                },
            },
            "required": ["command"],
        },
    },
}
