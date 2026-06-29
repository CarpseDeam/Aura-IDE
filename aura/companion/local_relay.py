"""Local Companion relay discovery and lifecycle helpers."""
from __future__ import annotations

import atexit
import errno
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

LOCAL_RELAY_PORT_MIN = 8765
LOCAL_RELAY_PORT_MAX = 8799
DEFAULT_LOCAL_RELAY_PORT = LOCAL_RELAY_PORT_MIN
AURA_RELAY_SERVICE = "aura-relay"
PORT_COLLISION_MESSAGE = (
    "Port {port} is already in use by another service. Close that service or change the "
    "Companion relay port in Advanced / Self-hosting."
)
STARTUP_FAILURE_MESSAGE = (
    "Aura could not find an available local Companion port. Close other local services or "
    "change the Companion relay URL in Advanced / Self-hosting."
)

_managed_process: subprocess.Popen | None = None
_atexit_registered = False


@dataclass(frozen=True)
class RelayHealthResult:
    """Result of probing a Companion relay health endpoint."""

    ok: bool
    kind: str
    status_code: int | None = None
    payload: dict[str, Any] | None = None
    error: str = ""


class LocalRelayError(RuntimeError):
    """Base class for local Companion relay startup failures."""


class LocalRelayPortCollisionError(LocalRelayError):
    """Raised when localhost:port is serving something other than Aura Relay."""


class LocalRelayStartupError(LocalRelayError):
    """Raised when Aura cannot launch its bundled local relay."""


def normalize_relay_url(url: str) -> str:
    """Return a WebSocket relay URL with a scheme and `/ws` endpoint."""
    value = (url or "").strip()
    if not value:
        value = f"ws://localhost:{DEFAULT_LOCAL_RELAY_PORT}"
    if "://" not in value:
        value = f"ws://{value}"

    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme == "http":
        scheme = "ws"
    elif scheme == "https":
        scheme = "wss"
    elif scheme not in {"ws", "wss"}:
        scheme = "ws"

    path = parsed.path or ""
    if not path or path == "/":
        path = "/ws"
    elif not path.rstrip("/").endswith("/ws"):
        path = path.rstrip("/") + "/ws"

    return urlunparse((scheme, parsed.netloc, path, "", parsed.query, ""))


def relay_health_url(url: str) -> str:
    """Convert a relay WebSocket URL into its HTTP health endpoint."""
    normalized = normalize_relay_url(url)
    parsed = urlparse(normalized)
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunparse((scheme, parsed.netloc, "/health", "", "", ""))


def is_local_relay_url(url: str) -> bool:
    """Return True for the default local Companion relay mode."""
    parsed = urlparse(normalize_relay_url(url))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "ws" and host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def relay_port(url: str) -> int:
    parsed = urlparse(normalize_relay_url(url))
    if parsed.port is not None:
        return parsed.port
    return DEFAULT_LOCAL_RELAY_PORT


def relay_host(url: str) -> str:
    """Return the normalized relay hostname."""
    parsed = urlparse(normalize_relay_url(url))
    return parsed.hostname or "localhost"


def with_relay_port(url: str, port: int) -> str:
    """Return a normalized relay URL using the given port."""
    normalized = normalize_relay_url(url)
    parsed = urlparse(normalized)
    host = relay_host(normalized)
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]:{port}"
    else:
        netloc = f"{host}:{port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", parsed.query, ""))


def iter_local_relay_candidates(url: str) -> list[str]:
    """Return bounded localhost relay candidates, starting with the configured port."""
    normalized = normalize_relay_url(url)
    if not is_local_relay_url(normalized):
        return [normalized]

    configured_port = relay_port(normalized)
    ports = [configured_port]
    ports.extend(
        port
        for port in range(LOCAL_RELAY_PORT_MIN, LOCAL_RELAY_PORT_MAX + 1)
        if port != configured_port
    )
    return [with_relay_port(normalized, port) for port in ports]


def probe_relay_health(url: str, *, timeout: float = 1.0) -> RelayHealthResult:
    """Probe `/health` and identify whether the server is Aura Relay."""
    health_url = relay_health_url(url)
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            body = response.read(65536)
    except urllib.error.HTTPError as exc:
        return RelayHealthResult(False, "wrong_server", status_code=exc.code, error=str(exc))
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if _is_connection_refused(reason):
            return RelayHealthResult(False, "unreachable", error=str(reason))
        return RelayHealthResult(False, "unreachable", error=str(reason))
    except OSError as exc:
        if _is_connection_refused(exc):
            return RelayHealthResult(False, "unreachable", error=str(exc))
        return RelayHealthResult(False, "unreachable", error=str(exc))
    except Exception as exc:
        return RelayHealthResult(False, "unreachable", error=str(exc))

    if status_code != 200:
        return RelayHealthResult(False, "wrong_server", status_code=status_code)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        return RelayHealthResult(False, "wrong_server", status_code=status_code, error=str(exc))

    if not isinstance(payload, dict):
        return RelayHealthResult(False, "wrong_server", status_code=status_code)
    if _is_aura_relay_payload(payload):
        return RelayHealthResult(True, "ok", status_code=status_code, payload=payload)
    return RelayHealthResult(False, "wrong_server", status_code=status_code, payload=payload)


def ensure_local_relay(url: str, *, startup_timeout: float = 5.0) -> str:
    """Ensure the local relay is available, starting Aura's child process if needed."""
    normalized = normalize_relay_url(url)
    if not is_local_relay_url(normalized):
        return normalized

    for candidate in iter_local_relay_candidates(normalized):
        port = relay_port(candidate)
        health = probe_relay_health(candidate)
        if health.ok:
            return candidate
        if health.kind == "wrong_server":
            logger.info("[Companion] local relay candidate %s is owned by another service", candidate)
            continue

        if _managed_process is not None and _managed_process.poll() is None:
            stop_managed_relay()
        try:
            _start_relay_process(port)
        except Exception:
            logger.exception("[Companion] failed to start local relay process on port %s", port)
            stop_managed_relay()
            continue

        deadline = time.monotonic() + startup_timeout
        last_health = health
        while time.monotonic() < deadline:
            time.sleep(0.15)
            if _managed_process is not None and _managed_process.poll() is not None:
                logger.error("[Companion] local relay exited with code %s", _managed_process.returncode)
                _clear_managed_process()
                break
            last_health = probe_relay_health(candidate)
            if last_health.ok:
                return candidate
            if last_health.kind == "wrong_server":
                logger.info("[Companion] local relay candidate %s became owned by another service", candidate)
                stop_managed_relay()
                break
        else:
            logger.error("[Companion] local relay did not become healthy on port %s: %s", port, last_health)
            stop_managed_relay()

    raise LocalRelayStartupError(STARTUP_FAILURE_MESSAGE)


def stop_managed_relay(*, timeout: float = 2.0) -> None:
    """Stop the relay process started by this Aura instance, if any."""
    global _managed_process
    process = _managed_process
    _managed_process = None
    if process is None or process.poll() is not None:
        return
    logger.info("[Companion] stopping managed local relay pid=%s", process.pid)
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("[Companion] managed local relay did not exit after kill")


def _is_aura_relay_payload(payload: dict[str, Any]) -> bool:
    if payload.get("service") == AURA_RELAY_SERVICE and payload.get("status") == "ok":
        return True
    return (
        payload.get("status") == "ok"
        and isinstance(payload.get("online_desktops"), int)
        and isinstance(payload.get("online_phones"), int)
    )


def _is_connection_refused(exc: object) -> bool:
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {errno.ECONNREFUSED, 10061}
    return "connection refused" in str(exc).lower() or "actively refused" in str(exc).lower()


def _clear_managed_process() -> None:
    global _managed_process
    _managed_process = None


def _start_relay_process(port: int) -> None:
    global _managed_process, _atexit_registered
    cmd = _relay_command(port)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    logger.info("[Companion] starting local relay: %s", cmd)
    _managed_process = subprocess.Popen(cmd, **kwargs)
    if not _atexit_registered:
        atexit.register(stop_managed_relay)
        _atexit_registered = True


def _relay_command(port: int) -> list[str]:
    host = "127.0.0.1"
    if getattr(sys, "frozen", False):
        return [sys.executable, "--companion-relay-only", "--host", host, "--port", str(port)]
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "relay.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
