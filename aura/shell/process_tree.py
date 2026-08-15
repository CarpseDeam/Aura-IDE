"""Process-tree termination helpers for shell-owned subprocesses."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from threading import Thread
from typing import Any

from aura.config import get_subprocess_kwargs

_log = logging.getLogger(__name__)
_PROCESS_SHUTDOWN_GRACE_SECONDS = 1.0


def terminate_process_tree(
    process: subprocess.Popen[bytes],
    job: Any | None,
    reader_threads: list[Thread],
) -> None:
    """Terminate a process and descendants, then close pipes and readers."""
    try:
        if process.poll() is None:
            if os.name == "nt":
                if job is not None:
                    job.terminate()
                    job = None
                else:
                    _taskkill_tree(process)
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=_PROCESS_SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.kill()
                process.wait(timeout=_PROCESS_SHUTDOWN_GRACE_SECONDS)
        elif job is not None:
            job.close()
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=_PROCESS_SHUTDOWN_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        for reader in reader_threads:
            reader.join(timeout=0.25)


def _taskkill_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        subprocess.run(
            ["taskkill", "/pid", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            **get_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("PowerShell tree termination failed: %s", exc)


__all__ = ["terminate_process_tree"]
