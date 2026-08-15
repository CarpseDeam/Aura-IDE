from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aura.config import get_subprocess_kwargs
from aura.shell.powershell_protocol import (
    BoundaryCollection,
    OutputCallback,
    QueueItem,
    collect_boundaries,
    frame_command,
    new_markers,
    read_pipe,
)
from aura.shell.process_tree import terminate_process_tree

_log = logging.getLogger(__name__)
SubmissionCallback = Callable[[], None] | None


@dataclass(frozen=True)
class PowerShellCommandResult:
    ok: bool
    stdout: str
    stderr: str
    output: str
    exit_code: int | None
    cwd: str
    timed_out: bool = False
    cancelled: bool = False
    failure_class: str | None = None
    session_id: str = ""
    submitted: bool = False
    session_started: bool = False
    session_restarted: bool = False
    session_reset: bool = False


def resolve_powershell() -> str:
    candidates = [shutil.which("pwsh")]
    if os.name == "nt":
        candidates.extend(
            [
                os.path.join(os.environ.get("ProgramFiles", ""), "PowerShell", "7", "pwsh.exe"),
                os.path.join(
                    os.environ.get("SystemRoot", r"C:\Windows"),
                    "System32",
                    "WindowsPowerShell",
                    "v1.0",
                    "powershell.exe",
                ),
                shutil.which("powershell"),
            ]
        )
    else:
        candidates.append(shutil.which("powershell"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("Unable to resolve pwsh or Windows PowerShell")


class PowerShellSession:
    """Own one serialized persistent PowerShell process for a conversation."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        self._current_cwd = self._workspace_root
        self._process: subprocess.Popen[bytes] | None = None
        self._job: Any | None = None
        self._session_id = ""
        self._queue: queue.Queue[QueueItem] | None = None
        self._reader_threads: list[threading.Thread] = []
        self._ever_submitted = False
        self._lock = threading.Lock()

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def current_cwd(self) -> Path:
        return self._current_cwd

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def execute(
        self,
        command: str,
        *,
        timeout: int,
        cancel_event: Any = None,
        on_output: OutputCallback = None,
        on_submitted: SubmissionCallback = None,
    ) -> PowerShellCommandResult:
        with self._lock:
            return self._execute_locked(
                command,
                timeout=timeout,
                cancel_event=cancel_event,
                on_output=on_output,
                on_submitted=on_submitted,
            )

    def close(self) -> None:
        with self._lock:
            self._terminate_locked()
            self._reset_process_state()

    def _execute_locked(
        self,
        command: str,
        *,
        timeout: int,
        cancel_event: Any,
        on_output: OutputCallback,
        on_submitted: SubmissionCallback,
    ) -> PowerShellCommandResult:
        if cancel_event is not None and cancel_event.is_set():
            return self._invalidate_result(
                message="\n[CANCELLED: PowerShell session reset]\n",
                cancelled=True,
                failure_class="cancelled",
            )

        process_was_started = False
        try:
            process_was_started = self._ensure_started()
            process = self._process
            output_queue = self._queue
            if process is None or output_queue is None or process.stdin is None:
                raise RuntimeError("PowerShell session did not start")
            markers = new_markers()
            payload = frame_command(command, markers)
            process.stdin.write(payload.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, ConnectionError, OSError, RuntimeError, ValueError) as exc:
            _log.warning("PowerShell command submission failed: %s", exc)
            self._terminate_locked()
            return self._invalidate_result(
                message=f"\n[ERROR: PowerShell command was not submitted: {exc}; session reset]\n",
                exit_code=-1,
                failure_class="execution_failed",
            )

        session_started = process_was_started and not self._ever_submitted
        session_restarted = process_was_started and self._ever_submitted
        self._ever_submitted = True
        identity = self._session_id
        if on_submitted is not None:
            on_submitted()

        collection = collect_boundaries(
            process,
            output_queue,
            markers,
            timeout=timeout,
            cancel_event=cancel_event,
            on_output=on_output,
        )
        if collection.failure is not None:
            return self._collection_failure_result(
                process,
                collection,
                identity=identity,
                session_started=session_started,
                session_restarted=session_restarted,
                timeout=timeout,
                on_output=on_output,
            )

        record = collection.record
        if record is None:
            return self._collection_failure_result(
                process,
                BoundaryCollection(
                    stdout=collection.stdout,
                    stderr=collection.stderr,
                    output=collection.output,
                    failure="malformed_record",
                ),
                identity=identity,
                session_started=session_started,
                session_restarted=session_restarted,
                timeout=timeout,
                on_output=on_output,
            )

        cwd = str(record.get("cwd") or self._current_cwd)
        try:
            self._current_cwd = Path(cwd).resolve()
        except OSError:
            pass
        exit_code = _as_int(record.get("exit_code"), default=1)
        return PowerShellCommandResult(
            ok=exit_code == 0,
            stdout=collection.stdout,
            stderr=collection.stderr,
            output=collection.output,
            exit_code=exit_code,
            cwd=cwd,
            session_id=identity,
            submitted=True,
            session_started=session_started,
            session_restarted=session_restarted,
        )

    def _ensure_started(self) -> bool:
        if self.is_running:
            return False
        if self._process is not None:
            self._terminate_locked()
            self._reset_process_state()
        shell_path = resolve_powershell()
        kwargs: dict[str, Any] = {
            "args": [
                shell_path,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "-",
            ],
            "cwd": str(self._workspace_root),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "text": False,
            "bufsize": 0,
        }
        kwargs.update(get_subprocess_kwargs())
        if os.name == "nt":
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | int(subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(**kwargs)
        self._process = process
        self._session_id = f"ps-{uuid.uuid4().hex[:12]}"
        self._queue = queue.Queue()
        self._reader_threads = []
        if os.name == "nt":
            from aura.win_job import WindowsJob

            self._job = WindowsJob.try_assign(process)
        for stream, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            if pipe is not None:
                reader = threading.Thread(
                    target=read_pipe,
                    args=(stream, pipe, self._queue),
                    name=f"aura-powershell-{stream}",
                    daemon=True,
                )
                reader.start()
                self._reader_threads.append(reader)
        self._write_startup_encoding(process)
        return True

    @staticmethod
    def _write_startup_encoding(process: subprocess.Popen[bytes]) -> None:
        if process.stdin is None:
            raise RuntimeError("PowerShell stdin is unavailable")
        startup = (
            "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        )
        process.stdin.write(startup.encode("utf-8"))
        process.stdin.flush()

    def _collection_failure_result(
        self,
        process: subprocess.Popen[bytes],
        collection: BoundaryCollection,
        *,
        identity: str,
        session_started: bool,
        session_restarted: bool,
        timeout: int,
        on_output: OutputCallback,
    ) -> PowerShellCommandResult:
        failure = collection.failure or "malformed_record"
        exit_code = -1
        timed_out = failure == "timeout"
        cancelled = failure == "cancelled"
        if timed_out:
            exit_code = 124
            message = f"\n[ERROR: Command timed out after {timeout} seconds; PowerShell session reset]\n"
        elif cancelled:
            message = "\n[CANCELLED: PowerShell session reset]\n"
        elif failure == "malformed_record":
            message = "\n[ERROR: Malformed PowerShell completion record; session reset]\n"
        else:
            exit_code = process.returncode if process.returncode is not None else -1
            message = "\n[ERROR: PowerShell session exited unexpectedly; session reset]\n"
        self._terminate_locked()
        if on_output is not None:
            on_output(message)
        result = PowerShellCommandResult(
            ok=False,
            stdout=collection.stdout,
            stderr=collection.stderr,
            output=collection.output + message,
            exit_code=exit_code,
            cwd=str(self._workspace_root),
            timed_out=timed_out,
            cancelled=cancelled,
            failure_class=failure,
            session_id=identity,
            submitted=True,
            session_started=session_started,
            session_restarted=session_restarted,
            session_reset=True,
        )
        self._reset_process_state()
        return result

    def _invalidate_result(
        self,
        *,
        message: str,
        exit_code: int | None = None,
        cancelled: bool = False,
        failure_class: str,
    ) -> PowerShellCommandResult:
        identity = self._session_id
        self._terminate_locked()
        self._reset_process_state()
        return PowerShellCommandResult(
            ok=False,
            stdout="",
            stderr="",
            output=message,
            exit_code=exit_code,
            cwd=str(self._workspace_root),
            cancelled=cancelled,
            failure_class=failure_class,
            session_id=identity,
            session_reset=True,
        )

    def _terminate_locked(self) -> None:
        process = self._process
        if process is None:
            return
        job = self._job
        self._job = None
        terminate_process_tree(process, job, self._reader_threads)

    def _reset_process_state(self) -> None:
        self._process = None
        self._queue = None
        self._reader_threads = []
        self._job = None
        self._session_id = ""
        self._current_cwd = self._workspace_root


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["PowerShellCommandResult", "PowerShellSession", "resolve_powershell"]
