"""SandboxExecutor — runs terminal commands and dynamic tools in Docker/WASM containers.

Provides true OS-level isolation so AI-generated code cannot harm the host.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from aura.paths import safe_is_relative_to

logger = logging.getLogger(__name__)

SandboxMode = Literal["host", "docker", "wasm"]

# The Docker image used for sandboxed execution.
SANDBOX_DOCKER_IMAGE = "python:3.10-slim"

# Resource limits for Docker containers.
DOCKER_MEMORY_LIMIT = "2g"
DOCKER_CPU_LIMIT = "2"
DOCKER_PIDS_LIMIT = 200
HEARTBEAT_INTERVAL_SECONDS = 5.0
PROCESS_SHUTDOWN_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class SandboxResult:
    """Result of a sandboxed execution."""
    ok: bool
    stdout: str
    stderr: str
    exit_code: int
    failure_class: str | None = None
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class WatchResult:
    """Result of a run-and-watch cycle."""
    ok: bool
    survived_window: bool
    exited_early: bool
    error_detected: bool
    exit_code: int | None
    output: str
    failure_class: str | None = None
    cancelled: bool = False


def _has_traceback(output: str) -> bool:
    return "Traceback (most recent call last):" in output


def _resolve_windows_command_shell() -> str:
    """Resolve the CMD executable Aura will use for host commands."""
    candidates: list[str] = []
    comspec = str(os.environ.get("COMSPEC") or "").strip().strip('"')
    if comspec:
        candidates.append(comspec)
    system_root = str(os.environ.get("SystemRoot") or "").strip().strip('"')
    if system_root:
        candidates.append(str(Path(system_root) / "System32" / "cmd.exe"))
    discovered = shutil.which("cmd.exe")
    if discovered:
        candidates.append(discovered)

    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve())
    raise FileNotFoundError("Unable to resolve COMSPEC/cmd.exe for host command execution")


def _resolve_posix_command_shell() -> str:
    """Resolve the supported POSIX shell used for host commands."""
    for candidate in ("/bin/sh", shutil.which("sh")):
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("Unable to resolve /bin/sh for host command execution")


def _host_shell_invocation(command: str) -> tuple[str | list[str], str | None]:
    """Build an explicit shell invocation while preserving *command* verbatim.

    Windows needs CMD's native outer-quote convention.  Python's generic
    list-to-command-line conversion backslash-escapes embedded quotes, but
    CMD does not treat those backslashes as quote escapes.
    """
    if os.name == "nt":
        shell = _resolve_windows_command_shell()
        return f'"{shell}" /d /s /c "{command}"', shell
    return [_resolve_posix_command_shell(), "-c", command], None


def _is_missing_executable_output(output: str) -> bool:
    lowered = str(output or "").lower()
    return (
        "is not recognized as an internal or external command" in lowered
        or "command not found" in lowered
        or re.search(
            r"(?m)^(?:.*/)?[a-z]*sh:\s*(?:\d+:\s*)?.+:\s+not found\s*$",
            lowered,
        )
        is not None
    )


def classify_watch_outcome(
    still_running: bool,
    exit_code: int | None,
    output: str,
    window_seconds: int,
    *,
    require_survive_window: bool = False,
    failure_class: str | None = None,
    cancelled: bool = False,
) -> WatchResult:
    error_detected = _has_traceback(output)

    if still_running:
        if error_detected:
            return WatchResult(
                ok=False, survived_window=True, exited_early=False,
                error_detected=True, exit_code=None, output=output,
                failure_class=failure_class, cancelled=cancelled,
            )
        return WatchResult(
            ok=True, survived_window=True, exited_early=False,
            error_detected=False, exit_code=None, output=output,
            failure_class=failure_class, cancelled=cancelled,
        )

    if error_detected:
        return WatchResult(
            ok=False, survived_window=False, exited_early=True,
            error_detected=True, exit_code=exit_code, output=output,
            failure_class=failure_class, cancelled=cancelled,
        )

    if require_survive_window:
        return WatchResult(
            ok=False, survived_window=False, exited_early=True,
            error_detected=False, exit_code=exit_code, output=output,
            failure_class=failure_class, cancelled=cancelled,
        )

    if exit_code == 0:
        return WatchResult(
            ok=True, survived_window=False, exited_early=True,
            error_detected=False, exit_code=exit_code, output=output,
            failure_class=failure_class, cancelled=cancelled,
        )

    return WatchResult(
        ok=False, survived_window=False, exited_early=True,
        error_detected=False, exit_code=exit_code, output=output,
        failure_class=failure_class, cancelled=cancelled,
    )


class SandboxExecutor:
    """Executes code/commands in a configurable sandbox.

    Modes:
        "host" — runs directly on the host (current behavior).
        "docker" — runs inside a Docker container with strict limits.
        "wasm" — stub (NotImplementedError).
    """

    def __init__(        self,
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
        working_directory: Path | str | None = None,
    ) -> SandboxResult:
        """Execute a shell command in the sandbox, with optional streaming.

        Args:
            command: The shell command to execute.
            timeout: Maximum seconds before killing.
            cancel_event: Optional threading.Event for cancellation.
            on_output: Optional callable(str) for streaming output chunks.
            input_data: Optional string to pass to stdin.
            working_directory: Optional resolved directory inside workspace_root.

        Returns:
            SandboxResult with ok, stdout, stderr, exit_code.
        """
        cwd = self._resolve_working_directory(working_directory)
        if self._mode == "host":
            return self._run_host_terminal(command, timeout, cancel_event, on_output, input_data, cwd)
        elif self._mode == "docker":
            if not self.docker_available:
                return SandboxResult(
                    ok=False,
                    stdout="",
                    stderr="Docker is not available. Install Docker or switch sandbox_mode to 'host'.",
                    exit_code=-1,
                )
            return self._run_docker_terminal(command, timeout, cancel_event, on_output, input_data, cwd)
        elif self._mode == "wasm":
            return SandboxResult(
                ok=False,
                stdout="",
                stderr="WASM sandbox is not yet implemented. Use 'docker' or 'host' mode.",
                exit_code=-1,
            )

    def run_and_watch(
        self,
        command: str,
        window_seconds: int = 10,
        cancel_event: Any = None,
        on_output: Any = None,
        *,
        require_survive_window: bool = False,
        working_directory: Path | str | None = None,
    ) -> WatchResult:
        """Run a command in host mode and watch it for a time window.

        The process is allowed to run for up to window_seconds. If it is
        still running after the window, it is killed and the result reports
        survived_window=True. If it exits early, the result reports
        exited_early=True with the exit code.
        """
        cwd = self._resolve_working_directory(working_directory)

        try:
            proc = self._launch_host_command(command, cwd=cwd, stdin_enabled=False)
            # Close stdin immediately — safe no-op when stdin is None
            if proc.stdin:
                proc.stdin.close()

            result = self._stream_subprocess_output(
                proc,
                timeout=window_seconds,
                cancel_event=cancel_event,
                on_output=on_output,
                timeout_is_success=True,
            )
            if result.exit_code != 0 and _is_missing_executable_output(result.stdout):
                result = SandboxResult(
                    ok=False,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                    failure_class="execution_failed",
                )

            if result.ok and result.exit_code == -1:
                return classify_watch_outcome(
                    still_running=True,
                    exit_code=None,
                    output=result.stdout,
                    window_seconds=window_seconds,
                    require_survive_window=require_survive_window,
                    failure_class=result.failure_class,
                    cancelled=result.cancelled,
                )

            return classify_watch_outcome(
                still_running=False,
                exit_code=result.exit_code,
                output=result.stdout,
                window_seconds=window_seconds,
                require_survive_window=require_survive_window,
                failure_class=result.failure_class,
                cancelled=result.cancelled,
            )

        except Exception as exc:
            output = f"\n[ERROR: {type(exc).__name__}: {exc}]\n"
            if on_output is not None:
                on_output(output)
            return classify_watch_outcome(
                still_running=False,
                exit_code=-1,
                output=output,
                window_seconds=window_seconds,
                require_survive_window=require_survive_window,
                failure_class="execution_failed",
            )

    @staticmethod
    def _launch_interactive_terminal(
        command: str,
        workspace_root: Path,
    ) -> bool:
        """Launch a command in a new interactive terminal window (fire-and-forget).

        Returns immediately after launching — does NOT wait for the process to exit.

        Args:
            command: The shell command to run in the new terminal.
            workspace_root: Working directory for the terminal process.

        Returns:
            True if the terminal was launched successfully, False otherwise.
        """
        try:
            if sys.platform == "win32":
                # Use 'start' without /wait to launch in a new window without blocking.
                # The first quoted argument to 'start' is the window title.
                title = f"Aura Auth: {command}"
                subprocess.Popen(
                    ["cmd.exe", "/c", "start", "", title, "cmd.exe", "/c", command],
                    cwd=str(workspace_root),
                    shell=False,
                )
            elif sys.platform == "darwin":
                # macOS: use 'open' with Terminal.app
                subprocess.Popen(
                    ["open", "-a", "Terminal", command],
                    cwd=str(workspace_root),
                )
            else:
                # Linux: try common terminal emulators
                terminal_candidates = [
                    ["x-terminal-emulator", "-e"],
                    ["gnome-terminal", "--"],
                    ["xterm", "-e"],
                    ["konsole", "-e"],
                ]
                launched = False
                for term_cmd in terminal_candidates:
                    try:
                        subprocess.Popen(
                            term_cmd + [command],
                            cwd=str(workspace_root),
                        )
                        launched = True
                        break
                    except FileNotFoundError:
                        continue
                if not launched:
                    logger.warning(
                        "Could not launch interactive terminal. "
                        "No known terminal emulator found."
                    )
                    return False

            logger.info("Launched interactive terminal for command: %s", command)
            return True

        except Exception as exc:
            logger.warning("Failed to launch interactive terminal: %s", exc)
            return False

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
        working_directory: Path | None = None,
    ) -> SandboxResult:
        """Execute a host command through Aura's explicit platform shell."""
        cwd = working_directory or self._workspace_root

        try:
            proc = self._launch_host_command(
                command,
                cwd=cwd,
                stdin_enabled=input_data is not None,
            )
            if input_data is not None and proc.stdin:
                try:
                    proc.stdin.write(input_data)
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    # Process likely exited immediately. We'll capture its output below.
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

            result = self._stream_subprocess_output(
                proc,
                timeout=timeout,
                cancel_event=cancel_event,
                on_output=on_output,
            )
            if result.exit_code != 0 and _is_missing_executable_output(result.stdout):
                return SandboxResult(
                    ok=False,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                    failure_class="execution_failed",
                )
            return result
        except Exception as exc:
            output = f"\n[ERROR: {type(exc).__name__}: {exc}]\n"
            if on_output is not None:
                on_output(output)
            return SandboxResult(
                ok=False,
                stdout=output,
                stderr="",
                exit_code=-1,
                failure_class="execution_failed",
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
        working_directory: Path | None = None,
    ) -> list[str]:
        """Build the base `docker run` arguments.

        Args:
            read_only_rootfs: If True, mount container root filesystem as read-only
                (with /tmp as tmpfs for dynamic tools that need temporary scratch space).
        """
        ws = str(self._workspace_root.resolve())
        docker_cwd = str((working_directory or self._workspace_root).resolve())

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
            "-w", docker_cwd,
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
        working_directory: Path | None = None,
    ) -> SandboxResult:
        """Run a terminal command inside a Docker container with streaming.

        The workspace is mounted read-write (needed for pip install, pytest, etc.).
        """
        self._ensure_docker_image()

        docker_args = self._build_docker_base_args(
            read_only_rootfs=False,
            working_directory=working_directory,
        )
        # Run the command via bash -c inside the container
        cmd = docker_args + ["bash", "-c", command]

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
                try:
                    proc.stdin.write(input_data)
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

            return self._stream_subprocess_output(
                proc,
                timeout=timeout,
                cancel_event=cancel_event,
                on_output=on_output,
            )
        except Exception as exc:
            output = f"\n[ERROR: {type(exc).__name__}: {exc}]\n"
            if on_output is not None:
                on_output(output)
            return SandboxResult(
                ok=False,
                stdout=output,
                stderr="",
                exit_code=-1,
                failure_class="execution_failed",
            )

    def _launch_host_command(
        self,
        command: str,
        *,
        cwd: Path,
        stdin_enabled: bool,
    ) -> subprocess.Popen[str]:
        """Launch a command with one explicit, platform-owned shell contract.

        The original command is kept as one argument so quoting, built-ins,
        chaining, pipes, and redirects are interpreted by the selected shell.
        ``shell=False`` ensures Python never chooses or wraps that shell for us.
        """
        from aura.config import get_subprocess_kwargs

        invocation, executable = _host_shell_invocation(command)
        popen_kwargs: dict[str, Any] = {
            "shell": False,
            "cwd": str(cwd),
            "stdin": subprocess.PIPE if stdin_enabled else None,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        popen_kwargs.update(get_subprocess_kwargs())
        return subprocess.Popen(invocation, executable=executable, **popen_kwargs)

    def _resolve_working_directory(self, working_directory: Path | str | None) -> Path:
        root = self._workspace_root.resolve()
        if working_directory is None or str(working_directory).strip() == "":
            return root
        candidate = Path(working_directory)
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if not safe_is_relative_to(resolved, root):
            raise ValueError("working_directory must stay inside workspace_root")
        return resolved

    def _stream_subprocess_output(
        self,
        proc: subprocess.Popen[str],
        timeout: int,
        cancel_event: Any = None,
        on_output: Any = None,
        *,
        timeout_is_success: bool = False,
    ) -> SandboxResult:
        """Stream process output while enforcing cancellation and timeouts."""
        assert proc.stdout is not None

        output_lines: list[str] = []
        output_queue: queue.Queue[str | None] = queue.Queue()

        def emit(text: str) -> None:
            output_lines.append(text)
            if on_output is not None:
                on_output(text)

        def reader() -> None:
            try:
                for line in iter(proc.stdout.readline, ""):
                    output_queue.put(line)
            except Exception as exc:
                output_queue.put(f"\n[ERROR: {type(exc).__name__}: {exc}]\n")
            finally:
                output_queue.put(None)
                try:
                    proc.stdout.close()
                except Exception:
                    pass

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        start_time = time.monotonic()
        deadline = start_time + max(0, timeout)
        last_output_time = start_time
        last_heartbeat_time = start_time
        stream_closed = False

        while True:
            drained_output = False
            while True:
                try:
                    item = output_queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    stream_closed = True
                    break
                emit(item)
                last_output_time = time.monotonic()
                last_heartbeat_time = last_output_time
                drained_output = True

            if cancel_event is not None and cancel_event.is_set():
                self._stop_process(proc)
                emit("\n[CANCELLED]\n")
                self._drain_output_queue(output_queue, emit)
                return SandboxResult(
                    ok=False,
                    stdout="".join(output_lines),
                    stderr="",
                    exit_code=-1,
                    failure_class="cancelled",
                    cancelled=True,
                )

            now = time.monotonic()
            if now >= deadline and proc.poll() is None:
                self._stop_process(proc)
                if timeout_is_success:
                    emit(f"\n[watch window elapsed: {timeout}s]\n")
                else:
                    emit(f"\n[ERROR: Command timed out after {timeout} seconds]\n")
                self._drain_output_queue(output_queue, emit)
                if timeout_is_success:
                    return SandboxResult(
                        ok=True,
                        stdout="".join(output_lines),
                        stderr="",
                        exit_code=-1,
                    )
                return SandboxResult(
                    ok=False,
                    stdout="".join(output_lines),
                    stderr="",
                    exit_code=124,
                    failure_class="timeout",
                    timed_out=True,
                )

            if (
                proc.poll() is None
                and now - last_output_time >= HEARTBEAT_INTERVAL_SECONDS
                and now - last_heartbeat_time >= HEARTBEAT_INTERVAL_SECONDS
            ):
                elapsed = int(now - start_time)
                emit(
                    f"[still running: {elapsed}s / timeout {timeout}s]\n"
                )
                last_heartbeat_time = now
                drained_output = True

            if proc.poll() is not None and stream_closed:
                break

            if not drained_output:
                try:
                    item = output_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    stream_closed = True
                else:
                    emit(item)
                    last_output_time = time.monotonic()
                    last_heartbeat_time = last_output_time

        self._drain_output_queue(output_queue, emit)
        return SandboxResult(
            ok=proc.returncode == 0,
            stdout="".join(output_lines),
            stderr="",
            exit_code=proc.returncode,
        )

    def _drain_output_queue(
        self,
        output_queue: queue.Queue[str | None],
        emit: Any,
    ) -> None:
        """Drain any queued output after a process exits or is stopped."""
        while True:
            try:
                item = output_queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                return
            emit(item)

    def _stop_process(self, proc: subprocess.Popen[str]) -> None:
        """Terminate a process promptly, then kill if it does not exit."""
        try:
            if proc.poll() is not None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=PROCESS_SHUTDOWN_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=PROCESS_SHUTDOWN_GRACE_SECONDS)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=PROCESS_SHUTDOWN_GRACE_SECONDS)
            except Exception:
                pass



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
