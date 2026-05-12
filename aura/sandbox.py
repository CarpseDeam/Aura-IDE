"""SandboxExecutor — runs terminal commands and dynamic tools in Docker/WASM containers.

Provides true OS-level isolation so AI-generated code cannot harm the host.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SandboxMode = Literal["host", "docker", "wasm"]

# The Docker image used for sandboxed execution.
SANDBOX_DOCKER_IMAGE = "python:3.10-slim"

# Resource limits for Docker containers.
DOCKER_MEMORY_LIMIT = "2g"
DOCKER_CPU_LIMIT = "2"
DOCKER_PIDS_LIMIT = 200


@dataclass(frozen=True)
class SandboxResult:
    """Result of a sandboxed execution."""
    ok: bool
    stdout: str
    stderr: str
    exit_code: int


class SandboxExecutor:
    """Executes code/commands in a configurable sandbox.

    Modes:
        "host" — runs directly on the host (current behavior).
        "docker" — runs inside a Docker container with strict limits.
        "wasm" — stub (NotImplementedError).
    """

    def __init__(
        self,
        mode: SandboxMode = "host",
        workspace_root: Path | None = None,
        network_enabled: bool = True,
    ) -> None:
        self._mode: SandboxMode = mode
        self._workspace_root = workspace_root or Path.cwd()
        self._network_enabled = network_enabled
        self._docker_available: bool | None = None  # Lazy check

    # ---- public API ---------------------------------------------------------

    @property
    def mode(self) -> SandboxMode:
        return self._mode

    @property
    def docker_available(self) -> bool:
        """Check whether Docker is installed and the daemon is reachable."""
        if self._docker_available is None:
            self._docker_available = self._check_docker()
        return self._docker_available

    def run_dynamic_tool(
        self,
        file_path: Path,
        function_name: str,
        arguments: dict[str, Any],
        timeout: int = 30,
    ) -> SandboxResult:
        """Execute a dynamic tool function in the sandbox.

        Args:
            file_path: Path to the .py file containing the function.
            function_name: Name of the function to call.
            arguments: Keyword arguments (JSON-serializable).
            timeout: Maximum seconds before killing.
        """
        runner_script = _DYNAMIC_TOOL_RUNNER_TEMPLATE

        if self._mode == "host":
            return self._run_host_dynamic_tool(
                runner_script, file_path, function_name, arguments, timeout
            )
        elif self._mode == "docker":
            if not self.docker_available:
                return SandboxResult(
                    ok=False,
                    stdout="",
                    stderr="Docker is not available. Install Docker or switch sandbox_mode to 'host'.",
                    exit_code=-1,
                )
            return self._run_docker_dynamic_tool(
                runner_script, file_path, function_name, arguments, timeout
            )
        elif self._mode == "wasm":
            return SandboxResult(
                ok=False,
                stdout="",
                stderr="WASM sandbox is not yet implemented. Use 'docker' or 'host' mode.",
                exit_code=-1,
            )

    def run_terminal_command(
        self,
        command: str,
        timeout: int = 120,
        cancel_event: Any = None,
        on_output: Any = None,
        input_data: str | None = None,
    ) -> SandboxResult:
        """Execute a shell command in the sandbox, with optional streaming.

        Args:
            command: The shell command to execute.
            timeout: Maximum seconds before killing.
            cancel_event: Optional threading.Event for cancellation.
            on_output: Optional callable(str) for streaming output chunks.
            input_data: Optional string to pass to stdin.

        Returns:
            SandboxResult with ok, stdout, stderr, exit_code.
        """
        if self._mode == "host":
            return self._run_host_terminal(command, timeout, cancel_event, on_output, input_data)
        elif self._mode == "docker":
            if not self.docker_available:
                return SandboxResult(
                    ok=False,
                    stdout="",
                    stderr="Docker is not available. Install Docker or switch sandbox_mode to 'host'.",
                    exit_code=-1,
                )
            return self._run_docker_terminal(command, timeout, cancel_event, on_output, input_data)
        elif self._mode == "wasm":
            return SandboxResult(
                ok=False,
                stdout="",
                stderr="WASM sandbox is not yet implemented. Use 'docker' or 'host' mode.",
                exit_code=-1,
            )

    @staticmethod
    def _run_interactive_command(
        command: str,
        workspace_root: Path,
    ) -> SandboxResult:
        """Run a command in a new interactive terminal window.
        
        This is used for authentication flows (gcloud auth, gemini login, etc.)
        where the user needs to interact with a TUI or browser.
        """
        import sys
        import subprocess

        try:
            if sys.platform == "win32":
                # Use 'start' to launch in a new cmd window.
                # /wait ensures we block until the user finishes.
                # The first quoted argument to 'start' is the window title.
                title = f"Aura Auth: {command}"
                proc = subprocess.run(
                    ["cmd.exe", "/c", "start", "/wait", title, "cmd.exe", "/c", command],
                    cwd=str(workspace_root),
                    check=False,
                )
                return SandboxResult(
                    ok=proc.returncode == 0,
                    stdout="Interactive session finished.",
                    stderr="",
                    exit_code=proc.returncode,
                )
            else:
                # Unix: This is trickier as terminal emulators vary.
                # For now, we'll try common ones or just run in-place if possible.
                # A better approach would be to prompt the user to run it themselves
                # or use a specific terminal emulator.
                return SandboxResult(
                    ok=False,
                    stdout="",
                    stderr="Interactive auth not yet supported on this platform.",
                    exit_code=-1,
                )
        except Exception as exc:
            return SandboxResult(
                ok=False,
                stdout="",
                stderr=f"Failed to launch interactive terminal: {exc}",
                exit_code=-1,
            )

    # ---- host execution (current behavior) ----------------------------------

    def _run_host_dynamic_tool(
        self,
        runner_script: str,
        file_path: Path,
        function_name: str,
        arguments: dict[str, Any],
        timeout: int,
    ) -> SandboxResult:
        """Direct subprocess execution (current behavior, no sandbox)."""
        try:
            from aura.config import get_subprocess_kwargs
            proc = subprocess.run(
                [sys.executable, "-c", runner_script, str(file_path), function_name],
                input=json.dumps(arguments),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                cwd=str(self._workspace_root),
                **get_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                ok=False,
                stdout="",
                stderr="Dynamic tool timed out after {}s.".format(timeout),
                exit_code=-1,
            )
        return SandboxResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
        )

    def _run_host_terminal(
        self,
        command: str,
        timeout: int,
        cancel_event: Any = None,
        on_output: Any = None,
        input_data: str | None = None,
    ) -> SandboxResult:
        """Direct Popen execution (current behavior, no sandbox)."""
        from aura.config import get_subprocess_kwargs

        popen_kwargs: dict[str, Any] = {
            "shell": True,
            "cwd": str(self._workspace_root),
            "stdin": subprocess.PIPE if input_data else None,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "bufsize": 1,
        }
        extra = get_subprocess_kwargs()
        popen_kwargs.update(extra)

        output_lines: list[str] = []
        try:
            proc = subprocess.Popen(command, **popen_kwargs)
            
            if input_data and proc.stdin:
                proc.stdin.write(input_data)
                proc.stdin.close()

            assert proc.stdout is not None

            for line in iter(proc.stdout.readline, ""):
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.wait()
                    output_lines.append("\n[CANCELLED]\n")
                    return SandboxResult(
                        ok=False,
                        stdout="".join(output_lines),
                        stderr="",
                        exit_code=-1,
                    )
                output_lines.append(line)
                if on_output is not None:
                    on_output(line)

            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            output_lines.append(f"\n[ERROR: Command timed out after {timeout} seconds]\n")
        except Exception as exc:
            output_lines.append(f"\n[ERROR: {type(exc).__name__}: {exc}]\n")
            return SandboxResult(
                ok=False,
                stdout="".join(output_lines),
                stderr="",
                exit_code=-1,
            )

        return SandboxResult(
            ok=proc.returncode == 0,
            stdout="".join(output_lines),
            stderr="",
            exit_code=proc.returncode,
        )

    # ---- Docker execution ---------------------------------------------------

    def _check_docker(self) -> bool:
        """Check if Docker CLI is available and daemon is responsive."""
        if shutil.which("docker") is None:
            return False
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                **({} if sys.platform != "win32" else {"creationflags": subprocess.CREATE_NO_WINDOW}),
            )
            return result.returncode == 0
        except Exception:
            return False

    def _ensure_docker_image(self) -> None:
        """Pull the sandbox image if it's not already cached locally."""
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", SANDBOX_DOCKER_IMAGE],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                **({} if sys.platform != "win32" else {"creationflags": subprocess.CREATE_NO_WINDOW}),
            )
            if result.returncode != 0:
                # Image not found, pull it
                subprocess.run(
                    ["docker", "pull", SANDBOX_DOCKER_IMAGE],
                    check=True,
                    timeout=120,
                    **({} if sys.platform != "win32" else {"creationflags": subprocess.CREATE_NO_WINDOW}),
                )
        except Exception:
            pass  # Will fail later with a clearer error

    def _build_docker_base_args(
        self,
        read_only_rootfs: bool = False,
    ) -> list[str]:
        """Build the base `docker run` arguments.

        Args:
            read_only_rootfs: If True, mount container root filesystem as read-only
                (with /tmp as tmpfs for dynamic tools that need temporary scratch space).
        """
        ws = str(self._workspace_root.resolve())

        args = [
            "docker", "run",
            "--rm",                          # Remove container after exit
            f"--memory={DOCKER_MEMORY_LIMIT}",
            f"--cpus={DOCKER_CPU_LIMIT}",
            f"--pids-limit={DOCKER_PIDS_LIMIT}",
            "--cap-drop=ALL",                # Drop all Linux capabilities
            "--security-opt=no-new-privileges",
            "--stop-timeout=5",              # Fast kill on timeout
            "-v", f"{ws}:{ws}:{'ro' if read_only_rootfs else 'rw'}",
            "-w", ws,
        ]

        if read_only_rootfs:
            # Mount rootfs read-only with tmpfs for /tmp (needed for Python import machinery)
            args.extend(["--read-only", "--tmpfs", "/tmp:exec"])

        if not self._network_enabled:
            args.append("--network=none")

        args.append(SANDBOX_DOCKER_IMAGE)

        return args

    def _run_docker_dynamic_tool(
        self,
        runner_script: str,
        file_path: Path,
        function_name: str,
        arguments: dict[str, Any],
        timeout: int,
    ) -> SandboxResult:
        """Run a dynamic tool inside a Docker container.

        The runner script is passed via ``python -c`` to the container.
        The workspace is mounted read-only.
        """
        self._ensure_docker_image()

        docker_args = self._build_docker_base_args(read_only_rootfs=True)
        # The runner script is passed inline via `-c`
        cmd = docker_args + [
            "python", "-c", runner_script, str(file_path), function_name,
        ]

        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(arguments),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                **({} if sys.platform != "win32" else {"creationflags": subprocess.CREATE_NO_WINDOW}),
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                ok=False,
                stdout="",
                stderr="Dynamic tool timed out after {}s.".format(timeout),
                exit_code=-1,
            )
        except Exception as exc:
            return SandboxResult(
                ok=False,
                stdout="",
                stderr=f"Docker execution failed: {type(exc).__name__}: {exc}",
                exit_code=-1,
            )

        return SandboxResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
        )

    def _run_docker_terminal(
        self,
        command: str,
        timeout: int,
        cancel_event: Any = None,
        on_output: Any = None,
        input_data: str | None = None,
    ) -> SandboxResult:
        """Run a terminal command inside a Docker container with streaming.

        The workspace is mounted read-write (needed for pip install, pytest, etc.).
        """
        self._ensure_docker_image()

        docker_args = self._build_docker_base_args(read_only_rootfs=False)
        # Run the command via bash -c inside the container
        cmd = docker_args + ["bash", "-c", command]

        output_lines: list[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if input_data else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
                **({} if sys.platform != "win32" else {"creationflags": subprocess.CREATE_NO_WINDOW}),
            )
            
            if input_data and proc.stdin:
                proc.stdin.write(input_data)
                proc.stdin.close()

            assert proc.stdout is not None

            for line in iter(proc.stdout.readline, ""):
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.wait()
                    output_lines.append("\n[CANCELLED]\n")
                    return SandboxResult(
                        ok=False,
                        stdout="".join(output_lines),
                        stderr="",
                        exit_code=-1,
                    )
                output_lines.append(line)
                if on_output is not None:
                    on_output(line)

            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            output_lines.append(f"\n[ERROR: Command timed out after {timeout} seconds]\n")
        except Exception as exc:
            output_lines.append(f"\n[ERROR: {type(exc).__name__}: {exc}]\n")
            return SandboxResult(
                ok=False,
                stdout="".join(output_lines),
                stderr="",
                exit_code=-1,
            )

        return SandboxResult(
            ok=proc.returncode == 0,
            stdout="".join(output_lines),
            stderr="",
            exit_code=proc.returncode,
        )


# ---------------------------------------------------------------------------
# Runner script template for dynamic tools
# ---------------------------------------------------------------------------

_DYNAMIC_TOOL_RUNNER_TEMPLATE = r"""
import sys, json, importlib.util

file_path = sys.argv[1]
function_name = sys.argv[2]

try:
    raw_args = sys.stdin.read()
    parsed_args = json.loads(raw_args) if raw_args.strip() else {}
except json.JSONDecodeError as exc:
    print(json.dumps({"ok": False, "error": f"Invalid JSON arguments: {exc}"}))
    sys.exit(0)

try:
    spec = importlib.util.spec_from_file_location("dynamic_tool", file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, function_name)

    # Isolate stdout: redirect to stderr so tool print()s don't
    # pollute the JSON result channel.
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        result = func(**parsed_args)
    finally:
        sys.stdout = _real_stdout

    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
"""
