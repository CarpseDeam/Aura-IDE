"""Windows Job Object ownership for host terminal process trees.

A terminal command launched through ``cmd.exe`` is a wrapper shell: the
program the model asked for is a *grandchild*, and killing the wrapper alone
leaves the real process running — and with it any marker, write, or server it
was about to produce after a delay.  Timeout, cancellation, and stop must
terminate the complete descendant tree, not only the wrapper.

The reliable Windows mechanism is a Job Object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: every process assigned to the job is
terminated when the job is terminated (or closed), descendants included.
This module owns exactly that — a ctypes wrapper around the small kernel32
surface needed to create, assign, terminate, and close such a job.  It is
deliberately Windows-only; POSIX host commands get their own process-group
handling in :mod:`aura.sandbox`.

Assignment can fail with ``ERROR_ACCESS_DENIED`` when the launching process
itself is already inside a job that does not allow breakaway (CI runners do
this).  :meth:`WindowsJob.try_assign` treats that as "no job ownership
available" and returns ``None``; the sandbox then falls back to a
``taskkill /T`` tree kill on stop.  The kill guarantee is a best-effort
contract with two mechanisms, and both are exercised by tests.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Any

_log = logging.getLogger(__name__)

#: Terminate all processes in the job when the last job handle closes.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: int = 0x00002000
#: ``SetInformationJobObject`` information class for extended limits.
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: int = 9

_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
ERROR_ACCESS_DENIED = 5

_IO_COUNTERS = [
    ("ReadOperationCount", ctypes.c_ulonglong),
    ("WriteOperationCount", ctypes.c_ulonglong),
    ("OtherOperationCount", ctypes.c_ulonglong),
    ("ReadTransferCount", ctypes.c_ulonglong),
    ("WriteTransferCount", ctypes.c_ulonglong),
    ("OtherTransferCount", ctypes.c_ulonglong),
]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS_STRUCT(ctypes.Structure):
    _fields_ = list(_IO_COUNTERS)


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS_STRUCT),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> Any:
    return ctypes.windll.kernel32


class WindowsJob:
    """One owned Job Object with ``KILL_ON_JOB_CLOSE`` semantics."""

    def __init__(self) -> None:
        kernel32 = _kernel32()
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [
            wintypes.LPVOID,
            wintypes.LPCWSTR,
        ]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle: int | None = handle

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process_handle: int) -> bool:
        """Assign *process_handle* to this job.  Returns False when the OS refuses.

        Refusal is normal in environments where the launching process is
        already inside a non-breakaway job; the caller keeps its fallback
        tree-kill path in that case.
        """
        if self._handle is None:
            return False
        ok = _kernel32().AssignProcessToJobObject(self._handle, process_handle)
        if ok:
            return True
        error = ctypes.get_last_error()
        if error == ERROR_ACCESS_DENIED:
            _log.info(
                "job assignment refused (process already in a non-breakaway "
                "job); falling back to tree-kill on stop"
            )
        else:
            _log.warning("job assignment failed with error %s", error)
        return False

    def terminate(self, exit_code: int = 1) -> None:
        """Kill every process in the job tree, then release the handle."""
        if self._handle is None:
            return
        kernel32 = _kernel32()
        kernel32.TerminateJobObject(self._handle, exit_code)
        kernel32.CloseHandle(self._handle)
        self._handle = None

    def close(self) -> None:
        """Release the job handle without explicitly killing its processes.

        ``KILL_ON_JOB_CLOSE`` makes this equivalent to termination while any
        process is still assigned, so callers use it only after the process
        tree has already exited.
        """
        if self._handle is not None:
            _kernel32().CloseHandle(self._handle)
            self._handle = None

    @classmethod
    def try_assign(cls, proc: Any) -> "WindowsJob | None":
        """Create a job and assign *proc* to it; ``None`` when unavailable.

        Never raises: job creation or assignment failures degrade to the
        caller's fallback tree-kill path instead of failing the launch.
        """
        try:
            job = cls()
        except OSError as exc:
            _log.warning("could not create a Job Object: %s", exc)
            return None
        try:
            process_handle = int(getattr(proc, "_handle", 0) or 0)
        except (TypeError, ValueError):
            process_handle = 0
        if not process_handle:
            job.close()
            return None
        if job.assign(process_handle):
            return job
        job.close()
        return None


__all__ = ["WindowsJob"]
