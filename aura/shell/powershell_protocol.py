"""Framing and boundary collection for Aura's persistent PowerShell session."""

from __future__ import annotations

import codecs
import json
import queue
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal

POLL_SECONDS = 0.05
QueueItem = tuple[str, str | None]
OutputCallback = Callable[[str], None] | None
FailureReason = Literal["cancelled", "timeout", "shell_exited", "malformed_record"]


@dataclass(frozen=True)
class ProtocolMarkers:
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BoundaryCollection:
    stdout: str
    stderr: str
    output: str
    record: dict[str, Any] | None = None
    failure: FailureReason | None = None


def new_markers() -> ProtocolMarkers:
    token = uuid.uuid4().hex
    return ProtocolMarkers(
        stdout=f"__AURA_PS_STDOUT_{token}__",
        stderr=f"__AURA_PS_STDERR_{token}__",
    )


def frame_command(command: str, markers: ProtocolMarkers) -> str:
    """Wrap one command with unique completion records on both output streams."""
    return (
        "$global:LASTEXITCODE = 0\n"
        "$__aura_success = $true\n"
        "try {\n"
        f"{command}\n"
        "$__aura_success = $?\n"
        "} catch {\n"
        "  $_ | Out-String | Write-Error\n"
        "  $__aura_success = $false\n"
        "}\n"
        "$__aura_native_exit = $global:LASTEXITCODE\n"
        "$__aura_exit = if ($__aura_success) { "
        "if ($null -eq $__aura_native_exit) { 0 } "
        "else { [int]$__aura_native_exit } "
        "} elseif ($null -ne $__aura_native_exit -and $__aura_native_exit -ne 0) { "
        "[int]$__aura_native_exit } else { 1 }\n"
        "$__aura_cwd = (Get-Location).Path\n"
        "$__aura_record = @{ exit_code = $__aura_exit; cwd = $__aura_cwd } "
        "| ConvertTo-Json -Compress\n"
        f"[Console]::Out.WriteLine('{markers.stdout}' + $__aura_record)\n"
        f"[Console]::Error.WriteLine('{markers.stderr}')\n\n"
    )


def read_pipe(stream: str, pipe: Any, output_queue: queue.Queue[QueueItem]) -> None:
    """Decode a process pipe and publish chunks plus one EOF sentinel."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                break
            output_queue.put((stream, decoder.decode(chunk)))
        tail = decoder.decode(b"", final=True)
        if tail:
            output_queue.put((stream, tail))
    except (OSError, ValueError):
        pass
    finally:
        output_queue.put((stream, None))


def collect_boundaries(
    process: subprocess.Popen[bytes],
    output_queue: queue.Queue[QueueItem],
    markers: ProtocolMarkers,
    *,
    timeout: int,
    cancel_event: Any,
    on_output: OutputCallback,
) -> BoundaryCollection:
    """Collect one command until its stdout record and stderr boundary arrive."""
    started = time.monotonic()
    buffers = {"stdout": "", "stderr": ""}
    parts = {"stdout": [], "stderr": []}
    output_parts: list[str] = []
    seen: set[str] = set()
    eof: set[str] = set()
    record: dict[str, Any] | None = None
    malformed = False

    def emit(stream: str, text: str) -> None:
        if not text:
            return
        parts[stream].append(text)
        output_parts.append(text)
        if on_output is not None:
            on_output(text)

    def consume(stream: str, text: str | None) -> None:
        nonlocal record, malformed
        if text is None:
            eof.add(stream)
            return
        if stream in seen:
            return
        marker = markers.stdout if stream == "stdout" else markers.stderr
        buffers[stream] += text
        marker_index = buffers[stream].find(marker)
        if marker_index < 0:
            safe_length = max(0, len(buffers[stream]) - len(marker) + 1)
            if safe_length:
                emit(stream, buffers[stream][:safe_length])
                buffers[stream] = buffers[stream][safe_length:]
            return
        newline = buffers[stream].find("\n", marker_index + len(marker))
        if newline < 0:
            if marker_index:
                emit(stream, buffers[stream][:marker_index])
                buffers[stream] = buffers[stream][marker_index:]
            return
        emit(stream, buffers[stream][:marker_index])
        boundary_value = buffers[stream][marker_index + len(marker) : newline].strip()
        buffers[stream] = ""
        if stream == "stdout":
            try:
                parsed = json.loads(boundary_value)
            except json.JSONDecodeError:
                malformed = True
                return
            if not isinstance(parsed, dict):
                malformed = True
                return
            record = parsed
        elif boundary_value:
            malformed = True
            return
        seen.add(stream)

    def finish(failure: FailureReason | None = None) -> BoundaryCollection:
        for stream in ("stdout", "stderr"):
            if stream not in seen and buffers[stream]:
                marker = markers.stdout if stream == "stdout" else markers.stderr
                text = _without_boundary_tail(buffers[stream], marker)
                emit(stream, text)
                buffers[stream] = ""
        return BoundaryCollection(
            stdout="".join(parts["stdout"]),
            stderr="".join(parts["stderr"]),
            output="".join(output_parts),
            record=record,
            failure=failure,
        )

    while seen != {"stdout", "stderr"}:
        if cancel_event is not None and cancel_event.is_set():
            return finish("cancelled")
        if time.monotonic() - started >= max(1, timeout):
            return finish("timeout")
        try:
            stream, text = output_queue.get(timeout=POLL_SECONDS)
            consume(stream, text)
        except queue.Empty:
            if process.poll() is not None and eof == {"stdout", "stderr"}:
                return finish("shell_exited")
        if malformed:
            return finish("malformed_record")
    return finish()


def _without_boundary_tail(text: str, marker: str) -> str:
    """Remove a full or partially written protocol marker from buffered output."""
    marker_index = text.find(marker)
    if marker_index >= 0:
        text = text[:marker_index]
    for length in range(min(len(text), len(marker) - 1), 0, -1):
        if text.endswith(marker[:length]):
            return text[:-length]
    return text


__all__ = [
    "BoundaryCollection",
    "OutputCallback",
    "ProtocolMarkers",
    "QueueItem",
    "collect_boundaries",
    "frame_command",
    "new_markers",
    "read_pipe",
]
