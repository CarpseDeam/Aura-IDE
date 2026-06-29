from __future__ import annotations

import json
import urllib.error
from io import BytesIO

import pytest

import aura.companion.local_relay as lr


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


def test_wrong_server_health_returns_friendly_port_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lr, "_managed_process", None)
    monkeypatch.setattr(lr, "probe_relay_health", lambda _url: lr.RelayHealthResult(False, "wrong_server", 404))

    with pytest.raises(lr.LocalRelayPortCollisionError) as excinfo:
        lr.ensure_local_relay("ws://localhost:8765")

    assert str(excinfo.value) == (
        "Port 8765 is already in use by another service. Close that service or change the "
        "Companion relay port in Advanced / Self-hosting."
    )


def test_remote_relay_url_does_not_auto_start_local_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lr, "_managed_process", None)
    monkeypatch.setattr(lr, "probe_relay_health", lambda _url: (_ for _ in ()).throw(AssertionError("no probe")))
    monkeypatch.setattr(lr, "_start_relay_process", lambda _port: (_ for _ in ()).throw(AssertionError("no start")))

    assert lr.ensure_local_relay("wss://relay.example") == "wss://relay.example/ws"
