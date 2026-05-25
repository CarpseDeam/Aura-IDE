"""Comprehensive tests for aura/sandbox.py — SandboxExecutor and friends.

All subprocess calls are mocked; the autouse ``block_real_subprocess`` fixture
in conftest.py raises RuntimeError on any real subprocess.run call.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread
from unittest.mock import ANY, MagicMock, PropertyMock, call, patch

import pytest

from aura.sandbox import (
    DOCKER_CPU_LIMIT,
    DOCKER_MEMORY_LIMIT,
    DOCKER_PIDS_LIMIT,
    SANDBOX_DOCKER_IMAGE,
    SandboxExecutor,
    SandboxMode,
    SandboxResult,
    _DYNAMIC_TOOL_RUNNER_TEMPLATE,
)


# ===================================================================
# SandboxResult dataclass
# ===================================================================


class TestSandboxResult:
    """Coverage area 1: dataclass instantiation, frozen=True, all fields."""

    def test_instantiation(self):
        result = SandboxResult(ok=True, stdout="hello", stderr="", exit_code=0)
        assert result.ok is True
        assert result.stdout == "hello"
        assert result.stderr == ""
        assert result.exit_code == 0

    def test_frozen(self):
        result = SandboxResult(ok=False, stdout="a", stderr="b", exit_code=1)
        with pytest.raises((AttributeError, TypeError)):
            result.ok = True  # type: ignore[misc]

    def test_all_fields_false(self):
        result = SandboxResult(ok=False, stdout="", stderr="err", exit_code=-1)
        assert result.ok is False
        assert result.stdout == ""
        assert result.stderr == "err"
        assert result.exit_code == -1

    def test_repr(self):
        result = SandboxResult(ok=True, stdout="o", stderr="e", exit_code=0)
        r = repr(result)
        assert "SandboxResult" in r
        assert "ok=True" in r


# ===================================================================
# SandboxExecutor.__init__
# ===================================================================


class TestSandboxExecutorInit:
    """Coverage area 2: constructor defaults and custom values."""

    def test_defaults(self):
        executor = SandboxExecutor()
        assert executor._mode == "host"
        assert executor._workspace_root == Path.cwd()
        assert executor._network_enabled is True

    def test_custom_values(self, tmp_path: Path):
        ws = tmp_path / "custom"
        ws.mkdir()
        executor = SandboxExecutor(mode="docker", workspace_root=ws, network_enabled=False)
        assert executor._mode == "docker"
        assert executor._workspace_root == ws
        assert executor._network_enabled is False

    def test_workspace_root_none_falls_back_to_cwd(self):
        executor = SandboxExecutor(workspace_root=None)
        assert executor._workspace_root == Path.cwd()

    def test_workspace_root_keeps_path_type(self, tmp_path: Path):
        ws = tmp_path / "my_ws"
        ws.mkdir()
        executor = SandboxExecutor(workspace_root=ws)
        assert isinstance(executor._workspace_root, Path)
        assert executor._workspace_root == ws


# ===================================================================
# SandboxExecutor.mode property
# ===================================================================


class TestSandboxExecutorMode:
    """Coverage area 3: mode property."""

    def test_returns_stored_mode(self):
        executor = SandboxExecutor(mode="docker")
        assert executor.mode == "docker"

    def test_mode_changes_reflected(self):
        executor = SandboxExecutor(mode="host")
        assert executor.mode == "host"
        executor._mode = "wasm"
        assert executor.mode == "wasm"


# ===================================================================
# SandboxExecutor.docker_available property
# ===================================================================


class TestSandboxExecutorDockerAvailable:
    """Coverage area 4: lazy check with cached result."""

    def test_lazy_check_called_once(self):
        executor = SandboxExecutor()
        with patch.object(executor, "_check_docker", return_value=True) as mock_check:
            result1 = executor.docker_available
            result2 = executor.docker_available

        assert result1 is True
        assert result2 is True
        mock_check.assert_called_once()

    def test_caches_false_result(self):
        executor = SandboxExecutor()
        with patch.object(executor, "_check_docker", return_value=False) as mock_check:
            r1 = executor.docker_available
            r2 = executor.docker_available

        assert r1 is False
        assert r2 is False
        mock_check.assert_called_once()

    def test_initial_none(self):
        executor = SandboxExecutor()
        assert executor._docker_available is None


# ===================================================================
# run_dynamic_tool dispatching
# ===================================================================


class TestRunDynamicTool:
    """Coverage area 5: dispatch by mode."""

    def test_host_mode_dispatches_to_host_method(self):
        executor = SandboxExecutor(mode="host")
        with patch.object(executor, "_run_host_dynamic_tool") as mock_method:
            mock_method.return_value = SandboxResult(ok=True, stdout="ok", stderr="", exit_code=0)
            result = executor.run_dynamic_tool(
                file_path=Path("tool.py"),
                function_name="foo",
                arguments={"x": 1},
                timeout=10,
            )
        assert result.ok is True
        mock_method.assert_called_once()

    def test_docker_mode_available_dispatches_to_docker_method(self):
        executor = SandboxExecutor(mode="docker")
        with (
            patch.object(SandboxExecutor, "docker_available", new_callable=PropertyMock, return_value=True),
            patch.object(executor, "_run_docker_dynamic_tool") as mock_method,
        ):
            mock_method.return_value = SandboxResult(ok=True, stdout="ok", stderr="", exit_code=0)
            result = executor.run_dynamic_tool(
                file_path=Path("tool.py"),
                function_name="foo",
                arguments={"x": 1},
                timeout=10,
            )
        assert result.ok is True
        mock_method.assert_called_once()

    def test_docker_mode_unavailable_returns_error(self):
        executor = SandboxExecutor(mode="docker")
        with patch.object(SandboxExecutor, "docker_available", new_callable=PropertyMock, return_value=False):
            result = executor.run_dynamic_tool(
                file_path=Path("tool.py"),
                function_name="foo",
                arguments={},
                timeout=10,
            )
        assert result.ok is False
        assert "Docker is not available" in result.stderr
        assert result.exit_code == -1

    def test_wasm_mode_returns_not_implemented(self):
        executor = SandboxExecutor(mode="wasm")
        result = executor.run_dynamic_tool(
            file_path=Path("tool.py"),
            function_name="foo",
            arguments={},
            timeout=10,
        )
        assert result.ok is False
        assert "WASM" in result.stderr
        assert "not yet implemented" in result.stderr
        assert result.exit_code == -1


# ===================================================================
# run_terminal_command dispatching
# ===================================================================


class TestRunTerminalCommand:
    """Coverage area 6: dispatch by mode."""

    def test_host_mode_dispatches_to_host_method(self):
        executor = SandboxExecutor(mode="host")
        with patch.object(executor, "_run_host_terminal") as mock_method:
            mock_method.return_value = SandboxResult(ok=True, stdout="output", stderr="", exit_code=0)
            result = executor.run_terminal_command(
                command="echo hello",
                timeout=30,
                cancel_event=None,
                on_output=None,
            )
        assert result.ok is True
        mock_method.assert_called_once_with("echo hello", 30, None, None, ANY)

    def test_docker_mode_available_dispatches_to_docker_method(self):
        executor = SandboxExecutor(mode="docker")
        with (
            patch.object(SandboxExecutor, "docker_available", new_callable=PropertyMock, return_value=True),
            patch.object(executor, "_run_docker_terminal") as mock_method,
        ):
            mock_method.return_value = SandboxResult(ok=True, stdout="output", stderr="", exit_code=0)
            result = executor.run_terminal_command(
                command="ls",
                timeout=60,
                cancel_event=None,
                on_output=None,
            )
        assert result.ok is True
        mock_method.assert_called_once_with("ls", 60, None, None, ANY)

    def test_docker_mode_unavailable_returns_error(self):
        executor = SandboxExecutor(mode="docker")
        with patch.object(SandboxExecutor, "docker_available", new_callable=PropertyMock, return_value=False):
            result = executor.run_terminal_command(
                command="ls",
                timeout=60,
                cancel_event=None,
                on_output=None,
            )
        assert result.ok is False
        assert "Docker is not available" in result.stderr
        assert result.exit_code == -1

    def test_wasm_mode_returns_not_implemented(self):
        executor = SandboxExecutor(mode="wasm")
        result = executor.run_terminal_command(
            command="echo hi",
            timeout=30,
            cancel_event=None,
            on_output=None,
        )
        assert result.ok is False
        assert "WASM" in result.stderr
        assert "not yet implemented" in result.stderr
        assert result.exit_code == -1


# ===================================================================
# _run_host_dynamic_tool
# ===================================================================


class TestRunHostDynamicTool:
    """Coverage area 7: subprocess.run for dynamic tools on host."""

    def test_successful_execution(self):
        executor = SandboxExecutor()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"ok": true, "result": 42}'
        mock_proc.stderr = ""

        with (
            patch("aura.sandbox.subprocess.run", return_value=mock_proc) as mock_run,
            patch("aura.config.get_subprocess_kwargs", return_value={}),
        ):
            result = executor._run_host_dynamic_tool(
                runner_script="import sys; print(sys.argv)",
                file_path=Path("tool.py"),
                function_name="foo",
                arguments={"a": 1},
                timeout=30,
            )

        assert result.ok is True
        assert result.stdout == '{"ok": true, "result": 42}'
        assert result.stderr == ""
        assert result.exit_code == 0

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert sys.executable in args[0][0]
        assert args[0][1] == "-c"
        assert args[0][3] == str(Path("tool.py"))
        assert args[0][4] == "foo"
        assert kwargs["input"] == json.dumps({"a": 1})
        assert kwargs["timeout"] == 30
        assert kwargs["cwd"] == str(Path.cwd())

    def test_failed_execution(self):
        executor = SandboxExecutor()
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Error occurred"

        with (
            patch("aura.sandbox.subprocess.run", return_value=mock_proc),
            patch("aura.config.get_subprocess_kwargs", return_value={}),
        ):
            result = executor._run_host_dynamic_tool(
                runner_script="script",
                file_path=Path("tool.py"),
                function_name="foo",
                arguments={},
                timeout=30,
            )

        assert result.ok is False
        assert result.stderr == "Error occurred"
        assert result.exit_code == 1

    def test_timeout_expired(self):
        executor = SandboxExecutor()
        with (
            patch("aura.sandbox.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="test", timeout=30)),
            patch("aura.config.get_subprocess_kwargs", return_value={}),
        ):
            result = executor._run_host_dynamic_tool(
                runner_script="script",
                file_path=Path("tool.py"),
                function_name="foo",
                arguments={},
                timeout=30,
            )

        assert result.ok is False
        assert "timed out" in result.stderr
        assert result.exit_code == -1


# ===================================================================
# _run_host_terminal
# ===================================================================


class TestRunHostTerminal:
    """Coverage area 8: subprocess.Popen for terminal commands on host."""

    def test_successful_execution(self):
        executor = SandboxExecutor()
        mock_proc = MagicMock()
        expected = SandboxResult(ok=True, stdout="line1\nline2\n", stderr="", exit_code=0)

        with (
            patch("aura.sandbox.subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("aura.config.get_subprocess_kwargs", return_value={}),
            patch.object(executor, "_stream_subprocess_output", return_value=expected) as mock_stream,
        ):
            result = executor._run_host_terminal(
                command="echo hello",
                timeout=30,
                cancel_event=None,
                on_output=None,
            )

        assert result.ok is True
        assert result.stdout == "line1\nline2\n"
        assert result.exit_code == 0

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        assert args[0] == "echo hello"
        assert kwargs["shell"] is True
        assert kwargs["cwd"] == str(Path.cwd())
        mock_stream.assert_called_once_with(
            mock_proc,
            timeout=30,
            cancel_event=None,
            on_output=None,
        )

    def test_passes_input_data_to_stdin(self):
        executor = SandboxExecutor()
        mock_proc = MagicMock()
        expected = SandboxResult(ok=True, stdout="", stderr="", exit_code=0)

        with (
            patch("aura.sandbox.subprocess.Popen", return_value=mock_proc),
            patch("aura.config.get_subprocess_kwargs", return_value={}),
            patch.object(executor, "_stream_subprocess_output", return_value=expected),
        ):
            result = executor._run_host_terminal(
                command="python -c \"print('ok')\"",
                timeout=30,
                cancel_event=None,
                on_output=None,
                input_data="stdin payload",
            )

        assert result.ok is True
        mock_proc.stdin.write.assert_called_once_with("stdin payload")
        mock_proc.stdin.close.assert_called()

    def test_exception_during_execution(self):
        executor = SandboxExecutor()
        with (
            patch("aura.sandbox.subprocess.Popen", side_effect=RuntimeError("boom")),
            patch("aura.config.get_subprocess_kwargs", return_value={}),
        ):
            result = executor._run_host_terminal(
                command="bad command",
                timeout=30,
                cancel_event=None,
                on_output=None,
            )

        assert result.ok is False
        assert "[ERROR:" in result.stdout
        assert "RuntimeError" in result.stdout
        assert "boom" in result.stdout
        assert result.exit_code == -1


# ===================================================================
# _check_docker
# ===================================================================


class TestCheckDocker:
    """Coverage area 9: Docker availability check."""

    def test_docker_installed_and_responsive(self):
        executor = SandboxExecutor()
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("aura.sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch("aura.sandbox.subprocess.run", return_value=mock_proc) as mock_run,
        ):
            result = executor._check_docker()

        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["docker", "info", "--format", "{{.ServerVersion}}"]

    def test_docker_not_installed(self):
        executor = SandboxExecutor()
        with patch("aura.sandbox.shutil.which", return_value=None):
            result = executor._check_docker()

        assert result is False

    def test_docker_daemon_unresponsive(self):
        executor = SandboxExecutor()
        with (
            patch("aura.sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch("aura.sandbox.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker info", timeout=5)),
        ):
            result = executor._check_docker()

        assert result is False


# ===================================================================
# _ensure_docker_image
# ===================================================================


class TestEnsureDockerImage:
    """Coverage area 10: Docker image pull logic."""

    def test_image_already_exists(self):
        executor = SandboxExecutor()
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("aura.sandbox.subprocess.run", return_value=mock_proc) as mock_run,
        ):
            executor._ensure_docker_image()

        # Only one call — the inspect; no pull
        assert mock_run.call_count == 1
        args = mock_run.call_args[0][0]
        assert args[:3] == ["docker", "image", "inspect"]

    def test_image_missing_triggers_pull(self):
        executor = SandboxExecutor()
        mock_inspect = MagicMock()
        mock_inspect.returncode = 1
        mock_pull = MagicMock()
        mock_pull.returncode = 0

        with (
            patch("aura.sandbox.subprocess.run", side_effect=[mock_inspect, mock_pull]) as mock_run,
        ):
            executor._ensure_docker_image()

        assert mock_run.call_count == 2
        # Second call should be pull
        pull_args = mock_run.call_args_list[1][0][0]
        assert pull_args[:2] == ["docker", "pull"]

    def test_exception_during_inspect_silently_caught(self):
        executor = SandboxExecutor()
        with (
            patch("aura.sandbox.subprocess.run", side_effect=RuntimeError("boom")),
        ):
            # Should not raise
            executor._ensure_docker_image()


# ===================================================================
# _build_docker_base_args
# ===================================================================


class TestBuildDockerBaseArgs:
    """Coverage area 11: Docker base argument construction."""

    def test_returns_list_starting_with_docker_run(self):
        executor = SandboxExecutor()
        args = executor._build_docker_base_args()
        assert isinstance(args, list)
        assert args[0] == "docker"
        assert args[1] == "run"

    def test_includes_rm_flag(self):
        executor = SandboxExecutor()
        args = executor._build_docker_base_args()
        assert "--rm" in args

    def test_includes_resource_limits(self):
        executor = SandboxExecutor()
        args = executor._build_docker_base_args()
        assert f"--memory={DOCKER_MEMORY_LIMIT}" in args
        assert f"--cpus={DOCKER_CPU_LIMIT}" in args
        assert f"--pids-limit={DOCKER_PIDS_LIMIT}" in args

    def test_workspace_mount_rw_when_not_read_only(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        executor = SandboxExecutor(workspace_root=ws)
        args = executor._build_docker_base_args(read_only_rootfs=False)
        ws_resolved = str(ws.resolve())
        # Look for the mount arg after -v
        v_idx = args.index("-v")
        mount = args[v_idx + 1]
        assert f"{ws_resolved}:{ws_resolved}:rw" in mount

    def test_workspace_mount_ro_when_read_only(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        executor = SandboxExecutor(workspace_root=ws)
        args = executor._build_docker_base_args(read_only_rootfs=True)
        ws_resolved = str(ws.resolve())
        v_idx = args.index("-v")
        mount = args[v_idx + 1]
        assert f"{ws_resolved}:{ws_resolved}:ro" in mount

    def test_read_only_rootfs_adds_read_only_and_tmpfs(self):
        executor = SandboxExecutor()
        args = executor._build_docker_base_args(read_only_rootfs=True)
        assert "--read-only" in args
        assert "--tmpfs" in args
        tmpfs_idx = args.index("--tmpfs")
        assert args[tmpfs_idx + 1] == "/tmp:exec"

    def test_read_only_rootfs_false_no_read_only_flag(self):
        executor = SandboxExecutor()
        args = executor._build_docker_base_args(read_only_rootfs=False)
        assert "--read-only" not in args

    def test_network_enabled_true_no_network_none(self):
        executor = SandboxExecutor(network_enabled=True)
        args = executor._build_docker_base_args()
        assert "--network=none" not in args

    def test_network_enabled_false_adds_network_none(self):
        executor = SandboxExecutor(network_enabled=False)
        args = executor._build_docker_base_args()
        assert "--network=none" in args

    def test_includes_docker_image_at_end(self):
        executor = SandboxExecutor()
        args = executor._build_docker_base_args()
        assert args[-1] == SANDBOX_DOCKER_IMAGE

    def test_includes_security_options(self):
        executor = SandboxExecutor()
        args = executor._build_docker_base_args()
        assert "--cap-drop=ALL" in args
        assert "--security-opt=no-new-privileges" in args
        assert "--stop-timeout=5" in args


# ===================================================================
# _run_docker_dynamic_tool
# ===================================================================


class TestRunDockerDynamicTool:
    """Coverage area 12: Docker dynamic tool execution."""

    def test_successful_execution(self):
        executor = SandboxExecutor(mode="docker")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"ok": true}'
        mock_proc.stderr = ""

        with (
            patch.object(executor, "_ensure_docker_image") as mock_ensure,
            patch.object(executor, "_build_docker_base_args", return_value=["docker", "run", "--rm", "python:3.10-slim"]),
            patch("aura.sandbox.subprocess.run", return_value=mock_proc) as mock_run,
        ):
            result = executor._run_docker_dynamic_tool(
                runner_script="print('hello')",
                file_path=Path("tool.py"),
                function_name="foo",
                arguments={"x": 1},
                timeout=30,
            )

        assert result.ok is True
        assert result.stdout == '{"ok": true}'
        assert result.exit_code == 0
        mock_ensure.assert_called_once()

        # Verify the command includes python -c and runner script
        cmd = mock_run.call_args[0][0]
        assert cmd[-5:] == ["python", "-c", "print('hello')", str(Path("tool.py")), "foo"]

    def test_timeout_expired(self):
        executor = SandboxExecutor(mode="docker")
        with (
            patch.object(executor, "_ensure_docker_image"),
            patch.object(executor, "_build_docker_base_args", return_value=["docker", "run", "--rm", "python:3.10-slim"]),
            patch("aura.sandbox.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="test", timeout=30)),
        ):
            result = executor._run_docker_dynamic_tool(
                runner_script="script",
                file_path=Path("tool.py"),
                function_name="foo",
                arguments={},
                timeout=30,
            )

        assert result.ok is False
        assert "timed out" in result.stderr
        assert result.exit_code == -1

    def test_general_exception(self):
        executor = SandboxExecutor(mode="docker")
        with (
            patch.object(executor, "_ensure_docker_image"),
            patch.object(executor, "_build_docker_base_args", return_value=["docker", "run", "--rm", "python:3.10-slim"]),
            patch("aura.sandbox.subprocess.run", side_effect=RuntimeError("docker daemon error")),
        ):
            result = executor._run_docker_dynamic_tool(
                runner_script="script",
                file_path=Path("tool.py"),
                function_name="foo",
                arguments={},
                timeout=30,
            )

        assert result.ok is False
        assert "Docker execution failed" in result.stderr
        assert "RuntimeError" in result.stderr
        assert "docker daemon error" in result.stderr
        assert result.exit_code == -1


# ===================================================================
# _run_docker_terminal
# ===================================================================


class TestRunDockerTerminal:
    """Coverage area 13: Docker terminal command execution."""

    def test_successful_execution(self):
        executor = SandboxExecutor(mode="docker")
        mock_proc = MagicMock()
        expected = SandboxResult(ok=True, stdout="out1\nout2\n", stderr="", exit_code=0)

        with (
            patch.object(executor, "_ensure_docker_image"),
            patch.object(executor, "_build_docker_base_args", return_value=["docker", "run", "--rm", "python:3.10-slim"]),
            patch("aura.sandbox.subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch.object(executor, "_stream_subprocess_output", return_value=expected) as mock_stream,
        ):
            result = executor._run_docker_terminal(
                command="echo hello",
                timeout=30,
                cancel_event=None,
                on_output=None,
            )

        assert result.ok is True
        assert result.stdout == "out1\nout2\n"
        assert result.exit_code == 0

        # Verify bash -c wrapping
        cmd = mock_popen.call_args[0][0]
        assert cmd[-3:] == ["bash", "-c", "echo hello"]
        mock_stream.assert_called_once_with(
            mock_proc,
            timeout=30,
            cancel_event=None,
            on_output=None,
        )

    def test_exception_during_execution(self):
        executor = SandboxExecutor(mode="docker")
        with (
            patch.object(executor, "_ensure_docker_image"),
            patch.object(executor, "_build_docker_base_args", return_value=["docker", "run", "--rm", "python:3.10-slim"]),
            patch("aura.sandbox.subprocess.Popen", side_effect=RuntimeError("container error")),
        ):
            result = executor._run_docker_terminal(
                command="bad cmd",
                timeout=30,
                cancel_event=None,
                on_output=None,
            )

        assert result.ok is False
        assert "[ERROR:" in result.stdout
        assert "RuntimeError" in result.stdout
        assert "container error" in result.stdout
        assert result.exit_code == -1

    def test_uses_shared_streaming_helper(self):
        executor = SandboxExecutor(mode="docker")
        mock_proc = MagicMock()
        expected = SandboxResult(ok=False, stdout="[CANCELLED]\n", stderr="", exit_code=-1)

        with (
            patch.object(executor, "_ensure_docker_image"),
            patch.object(executor, "_build_docker_base_args", return_value=["docker", "run", "--rm", "python:3.10-slim"]),
            patch("aura.sandbox.subprocess.Popen", return_value=mock_proc),
            patch.object(executor, "_stream_subprocess_output", return_value=expected) as mock_stream,
        ):
            result = executor._run_docker_terminal(
                command="echo hi",
                timeout=30,
                cancel_event=None,
                on_output=None,
            )

        assert result == expected
        mock_stream.assert_called_once_with(
            mock_proc,
            timeout=30,
            cancel_event=None,
            on_output=None,
        )


# ===================================================================
# Real terminal streaming behavior
# ===================================================================


class TestTerminalStreamingBehavior:
    """Real-process coverage for timeout, heartbeat, streaming, and cancel."""

    @staticmethod
    def _python_command(code: str) -> str:
        return subprocess.list2cmdline([sys.executable, "-c", code])

    def test_silent_timeout_returns_promptly(self, tmp_path: Path):
        executor = SandboxExecutor(workspace_root=tmp_path)
        start = time.monotonic()

        result = executor._run_host_terminal(
            command=self._python_command("import time; time.sleep(5)"),
            timeout=1,
        )

        elapsed = time.monotonic() - start
        assert elapsed < 3.0
        assert result.ok is False
        assert "timed out" in result.stdout.lower()
        assert result.exit_code == -1

    def test_silent_heartbeat_emits_before_exit(self, tmp_path: Path):
        executor = SandboxExecutor(workspace_root=tmp_path)
        streamed: list[str] = []

        result = executor._run_host_terminal(
            command=self._python_command("import time; time.sleep(6)"),
            timeout=10,
            on_output=streamed.append,
        )

        assert result.ok is True
        assert any("[still running:" in chunk for chunk in streamed)

    def test_streaming_output_still_works(self, tmp_path: Path):
        executor = SandboxExecutor(workspace_root=tmp_path)
        streamed: list[str] = []

        result = executor._run_host_terminal(
            command=self._python_command(
                "import time; "
                "print('line1', flush=True); "
                "time.sleep(0.3); "
                "print('line2', flush=True); "
                "time.sleep(0.3); "
                "print('line3', flush=True)"
            ),
            timeout=5,
            on_output=streamed.append,
        )

        assert result.ok is True
        assert streamed[:3] == ["line1\n", "line2\n", "line3\n"]
        assert result.stdout == "line1\nline2\nline3\n"

    def test_cancellation_kills_process_promptly(self, tmp_path: Path):
        executor = SandboxExecutor(workspace_root=tmp_path)
        cancel_event = Event()
        holder: dict[str, SandboxResult] = {}

        def run_command() -> None:
            holder["result"] = executor._run_host_terminal(
                command=self._python_command("import time; time.sleep(30)"),
                timeout=20,
                cancel_event=cancel_event,
            )

        start = time.monotonic()
        worker = Thread(target=run_command)
        worker.start()
        time.sleep(0.5)
        cancel_event.set()
        worker.join(timeout=3.0)
        elapsed = time.monotonic() - start

        assert not worker.is_alive()
        assert elapsed < 3.5
        result = holder["result"]
        assert result.ok is False
        assert "[CANCELLED]" in result.stdout
        assert result.exit_code == -1


# ===================================================================
# _DYNAMIC_TOOL_RUNNER_TEMPLATE sanity
# ===================================================================


class TestDynamicToolRunnerTemplate:
    """Coverage area 14: Template string sanity check."""

    def test_is_non_empty_string(self):
        assert isinstance(_DYNAMIC_TOOL_RUNNER_TEMPLATE, str)
        assert len(_DYNAMIC_TOOL_RUNNER_TEMPLATE) > 0

    def test_contains_expected_python_constructs(self):
        t = _DYNAMIC_TOOL_RUNNER_TEMPLATE
        assert "import sys" in t
        assert "import" in t and "json" in t  # json is imported: "import sys, json, importlib.util"
        assert "importlib" in t
        assert "sys.argv" in t
        assert "json.dumps" in t
        assert "sys.stdin.read()" in t

    def test_contains_function_execution_pattern(self):
        t = _DYNAMIC_TOOL_RUNNER_TEMPLATE
        assert "func(**parsed_args)" in t

    def test_contains_result_output_pattern(self):
        t = _DYNAMIC_TOOL_RUNNER_TEMPLATE
        assert 'json.dumps({"ok": True' in t or '"ok": True' in t


# ===================================================================
# Module constants
# ===================================================================


class TestModuleConstants:
    """Coverage area 15: Module-level constants exist and have expected types."""

    def test_sandbox_docker_image(self):
        assert isinstance(SANDBOX_DOCKER_IMAGE, str)
        assert len(SANDBOX_DOCKER_IMAGE) > 0
        assert "python" in SANDBOX_DOCKER_IMAGE

    def test_docker_memory_limit(self):
        assert isinstance(DOCKER_MEMORY_LIMIT, str)
        assert DOCKER_MEMORY_LIMIT.endswith("g")

    def test_docker_cpu_limit(self):
        assert isinstance(DOCKER_CPU_LIMIT, str)
        assert DOCKER_CPU_LIMIT.isdigit() or DOCKER_CPU_LIMIT.replace(".", "", 1).isdigit()

    def test_docker_pids_limit(self):
        assert isinstance(DOCKER_PIDS_LIMIT, int)
        assert DOCKER_PIDS_LIMIT > 0
