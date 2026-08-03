"""Single-owner GUI usage accounting tests.

One provider ``Usage`` event must update the conversation and displayed cost
totals exactly once.  The single authoritative path is the production
execution ledger:

    _Worker._on_event
      -> ProductionExecutionSession.handle_event
      -> WorkerEventRelay.usage
      -> workerUsage
      -> WorkerEventHandler._on_worker_usage   (the only accumulator mutation)

The old chat-facts path (``usageEmitted`` -> ``usageWithModel`` ->
``MainWindow._on_usage``) mutated the same accumulator a second time and has
been removed.  These tests feed ``Usage`` through the real production seam
(``_Worker._on_event``), so a re-introduced duplicate path fails them.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.bridge.qt_bridge import ConversationBridge, _Worker  # noqa: E402
from aura.client import Usage  # noqa: E402
from aura.gui.worker_handler import WorkerEventHandler  # noqa: E402
from aura.model_streams import PRODUCTION_STREAM_HOOK  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def bridge(monkeypatch, tmp_path) -> ConversationBridge:
    """A real bridge with the context system stubbed so the unit stays hermetic."""

    def _compose(*args, **kwargs):
        return SimpleNamespace(
            system_prompt="system",
            context_text="context",
            ledger=[],
        )

    monkeypatch.setattr("aura.bridge.qt_bridge.compose_system_prompt", _compose)
    monkeypatch.setattr(
        "aura.bridge.qt_bridge.context_gearbox_metadata",
        lambda *args, **kwargs: {"summary": {}, "ledger": []},
    )
    b = ConversationBridge(parent_widget=None, provider="test")
    b.set_workspace_root(tmp_path)
    return b


@pytest.fixture
def worker(bridge) -> _Worker:
    """The production worker exactly as ``ConversationBridge.send`` builds it."""
    return _Worker(
        manager=bridge._manager,
        approval_proxy=bridge._approval_proxy,
        dispatch_proxy=None,
        cancel_event=threading.Event(),
        model="test-model",
        thinking="off",
        temperature=0.7,
        production_session=bridge._production_session,
        hook_name=PRODUCTION_STREAM_HOOK,
    )


@pytest.fixture
def handler(bridge) -> WorkerEventHandler:
    h = WorkerEventHandler(
        bridge=bridge,
        chat=MagicMock(),
        playground=MagicMock(),
        settings=MagicMock(),
    )
    h.connect_bridge_signals()
    return h


def _usage(
    prompt: int = 100, completion: int = 25, hit: int = 40, miss: int = 60
) -> Usage:
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
    )


# ── 1 & 2: one event -> one update; distinct events each counted once ──────


class TestAccumulatorUpdates:
    def test_one_usage_event_updates_the_accumulator_exactly_once(
        self, bridge, worker, handler
    ) -> None:
        updates: list[int] = []
        handler.usage_updated.connect(lambda: updates.append(1))
        bridge._production_session.begin(model="test-model")

        worker._on_event(_usage())

        assert handler.session_usage == {
            "test-model": {"hit": 40, "miss": 60, "out": 25}
        }
        assert updates == [1]

    def test_repeated_distinct_usage_events_are_each_counted_once(
        self, bridge, worker, handler
    ) -> None:
        updates: list[int] = []
        handler.usage_updated.connect(lambda: updates.append(1))
        bridge._production_session.begin(model="test-model")

        worker._on_event(_usage(prompt=100, completion=25, hit=40, miss=60))
        worker._on_event(_usage(prompt=200, completion=50, hit=0, miss=200))

        assert handler.session_usage == {
            "test-model": {"hit": 40, "miss": 260, "out": 75}
        }
        assert updates == [1, 1]


# ── 3: relayed production events do not double-charge ──────────────────────


class TestNoDoubleCharge:
    def test_relayed_production_event_is_charged_once(self, bridge, worker, handler) -> None:
        """The real production seam used to fire both the ledger ``workerUsage``
        and the chat-facts ``usageWithModel`` mutation.  It must charge once."""
        bridge._production_session.begin(model="test-model")

        worker._on_event(_usage())

        assert handler.session_usage == {
            "test-model": {"hit": 40, "miss": 60, "out": 25}
        }

    def test_redundant_chat_facts_usage_signals_are_gone(self, bridge, worker) -> None:
        """No second accounting path may exist on the bridge or the worker."""
        assert not hasattr(bridge, "usageWithModel")
        assert not hasattr(bridge, "usageEmitted")
        assert not hasattr(worker, "usageEmitted")


# ── 4: cache split stays correctly attributed ──────────────────────────────


class TestCacheAttribution:
    def test_cache_hit_and_miss_remain_attributed(self, bridge, worker, handler) -> None:
        bridge._production_session.begin(model="test-model")

        worker._on_event(_usage(prompt=100, completion=25, hit=40, miss=60))

        bucket = handler.session_usage["test-model"]
        assert bucket["hit"] == 40
        assert bucket["miss"] == 60
        assert bucket["out"] == 25

    def test_no_cache_split_falls_back_to_prompt_as_miss(self, bridge, worker, handler) -> None:
        """Servers without the cache split keep the existing meter fallback."""
        bridge._production_session.begin(model="test-model")

        worker._on_event(_usage(prompt=100, completion=25, hit=0, miss=0))

        bucket = handler.session_usage["test-model"]
        assert bucket["hit"] == 0
        assert bucket["miss"] == 100
        assert bucket["out"] == 25


# ── 5: reset / new-turn contract ───────────────────────────────────────────


class TestResetAndNewTurn:
    def test_usage_carries_across_turns_and_models(self, bridge, worker, handler) -> None:
        """Totals carry between turns (no per-turn reset) and per-model."""
        bridge._production_session.begin(model="test-model")
        worker._on_event(_usage(prompt=100, completion=25, hit=40, miss=60))

        bridge._production_session.begin(model="test-model")
        worker._on_event(_usage(prompt=200, completion=50, hit=0, miss=200))

        bridge._production_session.begin(model="other-model")
        worker._on_event(_usage(prompt=10, completion=5, hit=0, miss=10))

        assert handler.session_usage == {
            "test-model": {"hit": 40, "miss": 260, "out": 75},
            "other-model": {"hit": 0, "miss": 10, "out": 5},
        }

    def test_reset_clears_totals_and_notifies_listeners(self, handler) -> None:
        updates: list[int] = []
        handler.usage_updated.connect(lambda: updates.append(1))
        handler._on_worker_usage("t1", "m1", 100, 25, 40, 60)

        handler.reset_session_usage()

        assert handler.session_usage == {}
        assert updates == [1, 1]
