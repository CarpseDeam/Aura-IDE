"""Focused coverage for durable conversation footer telemetry."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from aura.conversation.history import History
from aura.conversation.persistence import SCHEMA_VERSION, load_conversation, save_conversation
from aura.conversation.telemetry import ConversationTelemetry
from aura.gui.conv_persistence import ConversationPersistence
from aura.gui.execution_handler import ExecutionEventHandler


def test_telemetry_round_trips_through_conversation_json(tmp_path) -> None:
    history = History()
    history.append_user_text("hello")
    telemetry = ConversationTelemetry()
    telemetry.record_usage(
        model_id="deepseek-v4-pro",
        prompt=556_000,
        completion=1_413,
        hit=490_240,
        miss=65_880,
        context_window_tokens=1_000_000,
    )

    path = save_conversation(history, tmp_path, "deepseek-v4-pro", "off", telemetry=telemetry)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["version"] == SCHEMA_VERSION == 3
    assert payload["telemetry"] == {
        "per_model": {"deepseek-v4-pro": {"hit": 490_240, "miss": 65_880, "out": 1_413}},
        "latest_context": {
            "model_id": "deepseek-v4-pro",
            "input_tokens": 556_000,
            "context_window_tokens": 1_000_000,
        },
    }
    assert load_conversation(path).telemetry.to_dict() == payload["telemetry"]


def test_legacy_conversation_loads_zero_telemetry(tmp_path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"version": 2, "model": "model", "messages": []}), encoding="utf-8")

    assert load_conversation(path).telemetry.to_dict() == {
        "per_model": {},
        "latest_context": {"model_id": "", "input_tokens": 0, "context_window_tokens": 0},
    }


def test_execution_usage_accumulates_but_latest_context_is_replaced() -> None:
    handler = ExecutionEventHandler(
        bridge=object(), chat=object(), playground=object(), settings=object()
    )

    handler._on_execution_usage("run", "deepseek-v4-pro", 100, 10, 40, 60)
    handler._on_execution_usage("run", "deepseek-v4-pro", 250, 20, 0, 0)

    assert handler.conversation_usage == {
        "deepseek-v4-pro": {"hit": 40, "miss": 310, "out": 30}
    }
    assert handler.conversation_telemetry.latest_context.to_dict() == {
        "model_id": "deepseek-v4-pro",
        "input_tokens": 250,
        "context_window_tokens": 1_000_000,
    }


class _RestoreBridge:
    def __init__(self) -> None:
        self.history = History()
        self.registry = SimpleNamespace(workspace_root=None)

    def set_system_prompt(self, _value) -> None: pass
    def set_temperature(self, _value) -> None: pass
    def set_production_provider(self, _value) -> None: pass
    def refresh_production_prompt(self) -> None: pass
    def clear_pre_execution_snapshot(self) -> None: pass
    def reset_history(self) -> None: self.history = History()


def test_apply_loaded_restores_telemetry_before_status_refresh(tmp_path: Path) -> None:
    restored: list[dict] = []
    bridge = _RestoreBridge()
    persistence = ConversationPersistence(
        bridge=bridge,
        chat=SimpleNamespace(reset=lambda: None),
        playground=SimpleNamespace(clear=lambda: None),
        input_panel=object(),
        left_pane=SimpleNamespace(
            populate_models=lambda _provider: None,
            set_production_model=lambda _model: None,
            set_production_thinking=lambda _thinking: None,
        ),
        settings=SimpleNamespace(system_prompt="", temperature=0.7, provider="deepseek"),
        get_conversation_telemetry=ConversationTelemetry,
        restore_conversation_telemetry=lambda telemetry: restored.append(telemetry.to_dict()),
        reset_conversation_usage=lambda: None,
    )
    persistence._render_chat_items = lambda _items: None
    history = History()
    telemetry = ConversationTelemetry.from_dict({
        "per_model": {"model": {"hit": 1, "miss": 2, "out": 3}},
        "latest_context": {"model_id": "model", "input_tokens": 5, "context_window_tokens": 10},
    })
    loaded = SimpleNamespace(
        history=history,
        path=tmp_path / "conversation.json",
        provider="deepseek",
        model="model",
        thinking="off",
        chat_items=[],
        telemetry=telemetry,
    )
    refreshed: list[bool] = []
    persistence.needs_status_refresh.connect(lambda: refreshed.append(True))

    persistence.apply_loaded(loaded)

    assert restored == [telemetry.to_dict()]
    assert refreshed == [True]


def test_new_conversation_resets_telemetry() -> None:
    resets: list[bool] = []
    bridge = _RestoreBridge()
    persistence = ConversationPersistence(
        bridge=bridge,
        chat=SimpleNamespace(reset=lambda: None),
        playground=SimpleNamespace(clear=lambda: None),
        input_panel=object(),
        left_pane=object(),
        settings=object(),
        get_conversation_telemetry=ConversationTelemetry,
        restore_conversation_telemetry=lambda _telemetry: None,
        reset_conversation_usage=lambda: resets.append(True),
    )

    persistence.new_conversation()

    assert resets == [True]
