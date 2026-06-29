from __future__ import annotations

import json
import urllib.error
from io import BytesIO

import pytest

import aura.companion.local_relay as lr
import aura.companion.manager as manager_mod
from aura.settings import AppSettings


class _Response:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, _limit: int) -> bytes:
        return self._body


class _FakeProcess:
    returncode = None

    def poll(self):
        return self.returncode


def test_normalize_and_health_url_conversion() -> None:
    assert lr.normalize_relay_url("localhost:8765") == "ws://localhost:8765/ws"
    assert lr.normalize_relay_url("http://localhost:8765") == "ws://localhost:8765/ws"
    assert lr.normalize_relay_url("wss://relay.example/ws") == "wss://relay.example/ws"

    assert lr.relay_health_url("ws://localhost:8765/ws") == "http://localhost:8765/health"
    assert lr.relay_health_url("wss://relay.example/ws") == "https://relay.example/health"


def test_local_relay_url_detection() -> None:
    assert lr.is_local_relay_url("ws://localhost:8765")
    assert lr.is_local_relay_url("127.0.0.1:8765")
    assert not lr.is_local_relay_url("wss://localhost:8765")
    assert not lr.is_local_relay_url("ws://relay.example/ws")


def test_probe_relay_health_accepts_aura_health_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(url: str, timeout: float):
        assert url == "http://localhost:8765/health"
        return _Response(200, {"service": "aura-relay", "status": "ok", "online_desktops": 0, "online_phones": 0})

    monkeypatch.setattr(lr.urllib.request, "urlopen", fake_urlopen)

    result = lr.probe_relay_health("ws://localhost:8765/ws")

    assert result.ok is True
    assert result.kind == "ok"


def test_probe_relay_health_treats_404_as_wrong_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(url: str, timeout: float):
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=BytesIO(b""))

    monkeypatch.setattr(lr.urllib.request, "urlopen", fake_urlopen)

    result = lr.probe_relay_health("ws://localhost:8765/ws")

    assert result.ok is False
    assert result.kind == "wrong_server"
    assert result.status_code == 404


def test_health_ok_means_no_process_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lr, "_managed_process", None)
    monkeypatch.setattr(lr, "probe_relay_health", lambda _url: lr.RelayHealthResult(True, "ok"))

    def fail_start(_port: int) -> None:
        raise AssertionError("should not start process")

    monkeypatch.setattr(lr, "_start_relay_process", fail_start)

    assert lr.ensure_local_relay("ws://localhost:8765") == "ws://localhost:8765/ws"


def test_default_local_relay_returns_normalized_ws_when_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lr, "_managed_process", None)
    monkeypatch.setattr(lr, "probe_relay_health", lambda _url: lr.RelayHealthResult(True, "ok"))
    monkeypatch.setattr(
        lr,
        "_start_relay_process",
        lambda _port: (_ for _ in ()).throw(AssertionError("should not start process")),
    )

    assert lr.ensure_local_relay("ws://localhost:8765") == "ws://localhost:8765/ws"


def test_connection_refused_attempts_process_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    process = _FakeProcess()
    results = iter([
        lr.RelayHealthResult(False, "unreachable", error="connection refused"),
        lr.RelayHealthResult(True, "ok"),
    ])

    monkeypatch.setattr(lr, "_managed_process", None)
    monkeypatch.setattr(lr.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(lr, "probe_relay_health", lambda _url: next(results))

    def fake_start(port: int) -> None:
        calls.append(f"start:{port}")
        monkeypatch.setattr(lr, "_managed_process", process)

    monkeypatch.setattr(lr, "_start_relay_process", fake_start)

    assert lr.ensure_local_relay("ws://localhost:8765") == "ws://localhost:8765/ws"
    assert calls == ["start:8765"]


def test_wrong_server_falls_back_to_next_healthy_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lr, "_managed_process", None)
    seen: list[str] = []

    def fake_probe(url: str) -> lr.RelayHealthResult:
        seen.append(url)
        if url == "ws://localhost:8765/ws":
            return lr.RelayHealthResult(False, "wrong_server", 404)
        if url == "ws://localhost:8766/ws":
            return lr.RelayHealthResult(True, "ok")
        raise AssertionError(f"unexpected candidate: {url}")

    monkeypatch.setattr(lr, "probe_relay_health", fake_probe)
    monkeypatch.setattr(
        lr,
        "_start_relay_process",
        lambda _port: (_ for _ in ()).throw(AssertionError("should not start process")),
    )

    assert lr.ensure_local_relay("ws://localhost:8765") == "ws://localhost:8766/ws"
    assert seen == ["ws://localhost:8765/ws", "ws://localhost:8766/ws"]


def test_wrong_server_then_unreachable_starts_relay_on_next_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    process = _FakeProcess()
    probe_counts: dict[str, int] = {}

    def fake_probe(url: str) -> lr.RelayHealthResult:
        probe_counts[url] = probe_counts.get(url, 0) + 1
        if url == "ws://localhost:8765/ws":
            return lr.RelayHealthResult(False, "wrong_server", 404)
        if url == "ws://localhost:8766/ws" and probe_counts[url] == 1:
            return lr.RelayHealthResult(False, "unreachable", error="connection refused")
        if url == "ws://localhost:8766/ws":
            return lr.RelayHealthResult(True, "ok")
        raise AssertionError(f"unexpected candidate: {url}")

    def fake_start(port: int) -> None:
        calls.append(f"start:{port}")
        monkeypatch.setattr(lr, "_managed_process", process)

    monkeypatch.setattr(lr, "_managed_process", None)
    monkeypatch.setattr(lr.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(lr, "probe_relay_health", fake_probe)
    monkeypatch.setattr(lr, "_start_relay_process", fake_start)

    assert lr.ensure_local_relay("ws://localhost:8765") == "ws://localhost:8766/ws"
    assert calls == ["start:8766"]


def test_fallback_candidates_do_not_duplicate_configured_port() -> None:
    candidates = lr.iter_local_relay_candidates("ws://localhost:8766")

    assert candidates[0] == "ws://localhost:8766/ws"
    assert len(candidates) == len(set(candidates))
    assert candidates.count("ws://localhost:8766/ws") == 1
    assert "ws://localhost:8765/ws" in candidates


def test_remote_relay_url_does_not_auto_start_local_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lr, "_managed_process", None)
    monkeypatch.setattr(lr, "probe_relay_health", lambda _url: (_ for _ in ()).throw(AssertionError("no probe")))
    monkeypatch.setattr(lr, "_start_relay_process", lambda _port: (_ for _ in ()).throw(AssertionError("no start")))

    assert lr.ensure_local_relay("wss://relay.example") == "wss://relay.example/ws"


def test_manager_connect_uses_returned_runtime_relay_url(monkeypatch: pytest.MonkeyPatch) -> None:
    created_urls: list[str] = []
    connected_urls: list[str] = []

    class _Signal:
        def connect(self, _callback):
            return None

    class _Client:
        connected = _Signal()
        disconnected = _Signal()
        error = _Signal()
        message_received = _Signal()

        def __init__(self, url: str, _device_id: str, _desktop_secret: str, _parent) -> None:
            created_urls.append(url)
            self.url = url

        def connect_to_relay(self) -> None:
            connected_urls.append(self.url)

    settings = AppSettings(companion_enabled=True, companion_relay_url="ws://localhost:8765")
    manager = manager_mod.CompanionManager(settings)

    monkeypatch.setattr(manager_mod, "ensure_local_relay", lambda _url: "ws://localhost:8766/ws")
    monkeypatch.setattr(manager_mod, "get_device_id", lambda: "desktop-id")
    monkeypatch.setattr(manager_mod, "CompanionWsClient", _Client)

    manager._connect()

    assert manager._active_relay_url == "ws://localhost:8766/ws"
    assert created_urls == ["ws://localhost:8766/ws"]
    assert connected_urls == ["ws://localhost:8766/ws"]
    assert settings.companion_relay_url == "ws://localhost:8765"
