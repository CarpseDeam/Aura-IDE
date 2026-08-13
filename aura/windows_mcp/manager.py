"""Lifecycle for the Windows Computer Use server.

One object owns one connection to one server, driving the *existing*
:class:`~aura.conversation.tools.registry.ToolRegistry` MCP seam.  There is no
second registry here and no second tool table: registration, collision refusal,
effect resolution, approval, and disconnection all stay where they already are.
What this adds is the part the registry has no opinion about — when to connect,
what to connect to, and how to stop.

**Disabled means nothing happened.**  Not "connected but hidden", not
"downloaded and idle".  With the setting off, this class starts no process,
contacts no network, creates no directory, and registers no tool, so the model's
tool list is byte-identical to a build without the feature.

**Enabling does not block the GUI.**  Resolving an install can mean a GitHub
round trip and a fifty-megabyte download, and connecting launches a subprocess
and waits on its handshake.  All of it runs on a worker thread; the caller gets
an immediate ``connecting`` status and watches :meth:`status` change.

**Every path converges on one connection.**  :meth:`apply_settings` is
idempotent by construction: it computes the command the settings ask for,
compares it with what is connected, and does nothing when they already agree.
Re-enabling something already enabled is a no-op rather than a second process,
and a changed command tears the old one down before standing the new one up.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from typing import Any, Callable

from aura.windows_mcp.allowlist import (
    WINDOWS_MCP_CAPABILITY,
    filter_windows_tool_defs,
)
from aura.windows_mcp.install import (
    WindowsMcpInstallError,
    install_release,
    installed_server_path,
    remove_installations,
    resolve_server_command,
)

_log = logging.getLogger(__name__)

#: Lifecycle states surfaced to the settings page.
STATE_DISABLED = "disabled"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_ERROR = "error"


@dataclass(frozen=True)
class WindowsComputerUseStatus:
    """An immutable snapshot of the integration, safe to read from any thread."""

    state: str = STATE_DISABLED
    tool_count: int = 0
    version: str = ""
    path: str = ""
    error: str = ""
    detail: str = ""

    @property
    def is_connected(self) -> bool:
        return self.state == STATE_CONNECTED

    @property
    def installed(self) -> bool:
        return bool(self.path)


class WindowsComputerUseManager:
    """Connects, disconnects, and reports the Windows MCP server."""

    def __init__(
        self,
        registry: Any,
        *,
        on_status_changed: Callable[[WindowsComputerUseStatus], None] | None = None,
        install_resolver: Callable[..., str] = resolve_server_command,
    ) -> None:
        self._registry = registry
        self._on_status_changed = on_status_changed
        self._resolve_command = install_resolver
        # Guards the connection bookkeeping below.  Never held across the
        # registry calls that launch or close a subprocess: those can take
        # seconds, and :meth:`status` has to stay answerable while they run.
        self._lock = threading.RLock()
        self._connected_command: str = ""
        self._worker: threading.Thread | None = None
        # Bumped on every apply.  A worker whose generation is stale lost a
        # race with a newer decision and must not publish its result.
        self._generation = 0
        self._status = WindowsComputerUseStatus()
        self._refresh_install_fields()

    # ── status ──────────────────────────────────────────────────────────

    def status(self) -> WindowsComputerUseStatus:
        with self._lock:
            return self._status

    def _set_status(self, **fields: Any) -> None:
        with self._lock:
            self._status = replace(self._status, **fields)
            snapshot = self._status
        if self._on_status_changed is not None:
            try:
                self._on_status_changed(snapshot)
            except Exception:
                _log.exception("Windows Computer Use status listener failed")

    def _refresh_install_fields(self) -> None:
        """Record what is on disk without connecting to it."""
        existing = installed_server_path()
        version = "" if existing is None else existing.version
        path = "" if existing is None else str(existing.exe_path)
        with self._lock:
            self._status = replace(self._status, version=version, path=path)

    # ── lifecycle ───────────────────────────────────────────────────────

    def apply_settings(self, settings: Any) -> None:
        """Bring the connection in line with *settings*.

        The only entry point the GUI needs, and safe to call repeatedly with
        the same values.
        """
        enabled = bool(getattr(settings, "windows_computer_use_enabled", False))
        custom = str(getattr(settings, "windows_computer_use_command", "") or "").strip()

        if not enabled:
            self.shutdown()
            return

        with self._lock:
            self._generation += 1
            generation = self._generation
            already = self._connected_command
            execution_running = self._worker is not None and self._worker.is_alive()

        # A custom command is known before any work happens, so an unchanged
        # one can be recognised as already-satisfied without touching the disk
        # or the network.  A managed command is only known after resolution,
        # so it is compared on the worker thread instead.
        if custom and already == custom:
            return
        if not custom and already and not execution_running:
            resolved_same = self._managed_command_matches(already)
            if resolved_same:
                return

        self._set_status(state=STATE_CONNECTING, error="", detail="")
        worker = threading.Thread(
            target=self._connect_worker,
            args=(custom, generation),
            name="windows-mcp-connect",
            daemon=True,
        )
        with self._lock:
            self._worker = worker
        worker.start()

    def _managed_command_matches(self, connected_command: str) -> bool:
        """Whether an installed managed server is what is already connected."""
        existing = installed_server_path()
        if existing is None:
            return False
        return connected_command.strip('"') == str(existing.exe_path)

    def _connect_worker(self, custom_command: str, generation: int) -> None:
        try:
            command = self._resolve_command(
                custom_command,
                progress=lambda message: self._publish_detail(message, generation),
            )
        except Exception as exc:
            self._fail(generation, f"{exc}")
            return

        if self._is_stale(generation):
            return

        # Tearing down first keeps "one connection" true even when the command
        # changed, and makes a reconnect leave no orphan behind.
        self._disconnect_current()

        try:
            count = self._registry.connect_mcp_server(
                command,
                tool_filter=filter_windows_tool_defs,
                capability=WINDOWS_MCP_CAPABILITY,
            )
        except Exception as exc:
            # A rejected registration already closed its own client, so there
            # is no process to clean up here — only state to keep honest.
            _log.warning("Windows Computer Use connection failed: %s", exc)
            self._fail(generation, f"{exc}")
            return

        if self._is_stale(generation):
            # A newer decision landed while the handshake was in flight. This
            # connection is nobody's intent, so close it now rather than leave
            # a live subprocess nothing will ever disconnect.
            self._disconnect_command(command)
            return

        with self._lock:
            self._connected_command = command
        self._refresh_install_fields()
        self._set_status(
            state=STATE_CONNECTED,
            tool_count=count,
            error="",
            detail="",
        )
        _log.info(
            "windows_computer_use_connected tools=%d command=%s", count, command
        )

    def shutdown(self) -> None:
        """Disconnect, close the process, and drop every trace of the server.

        Idempotent, and correct to call when nothing is connected — which is
        what makes it usable as both the disable path and the app-exit path.
        """
        with self._lock:
            self._generation += 1
        self._disconnect_current()
        self._set_status(
            state=STATE_DISABLED,
            tool_count=0,
            error="",
            detail="",
        )

    def _disconnect_current(self) -> None:
        with self._lock:
            command = self._connected_command
            self._connected_command = ""
        if command:
            self._disconnect_command(command)

    def _disconnect_command(self, command: str) -> None:
        """Remove exactly this server's tools and close exactly its process.

        Both are the registry's own per-server bookkeeping: it unregisters the
        names it recorded for this command and closes the client it opened for
        it, which drops the capability tag in the same step — so the
        request-time context block disappears with the tools rather than
        needing to be cleared separately.
        """
        try:
            removed = self._registry.disconnect_mcp_server(command)
            _log.info("windows_computer_use_disconnected tools=%d", removed)
        except Exception:
            _log.exception("Failed to disconnect the Windows Computer Use server")

    # ── managed installation actions (settings page buttons) ────────────

    def install_or_repair(self, *, force: bool = False) -> WindowsComputerUseStatus:
        """Install the official release, or reinstall it when *force*.

        Synchronous: the settings page runs it on its own worker and shows
        progress, and a caller that wants the connection updated afterwards
        calls :meth:`apply_settings` again.
        """
        try:
            install_release(
                force=force,
                progress=lambda message: self._set_status(detail=message),
            )
        except WindowsMcpInstallError as exc:
            self._set_status(state=STATE_ERROR, error=str(exc), detail="")
            return self.status()
        self._refresh_install_fields()
        self._set_status(error="", detail="")
        return self.status()

    def remove_installation(self) -> WindowsComputerUseStatus:
        """Disconnect and delete every managed install."""
        self.shutdown()
        try:
            removed = remove_installations()
        except OSError as exc:
            self._set_status(state=STATE_ERROR, error=str(exc))
            return self.status()
        self._refresh_install_fields()
        self._set_status(detail=f"Removed {removed} installation(s).", error="")
        return self.status()

    # ── worker plumbing ─────────────────────────────────────────────────

    def _is_stale(self, generation: int) -> bool:
        with self._lock:
            return generation != self._generation

    def _publish_detail(self, message: str, generation: int) -> None:
        if not self._is_stale(generation):
            self._set_status(detail=message)

    def _fail(self, generation: int, error: str) -> None:
        if self._is_stale(generation):
            return
        self._set_status(state=STATE_ERROR, error=error, detail="")


__all__ = [
    "STATE_CONNECTED",
    "STATE_CONNECTING",
    "STATE_DISABLED",
    "STATE_ERROR",
    "WindowsComputerUseManager",
    "WindowsComputerUseStatus",
]
