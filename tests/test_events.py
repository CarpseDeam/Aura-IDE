"""Tests for the foundation event bus (aura.events)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import time

import pytest

from aura.events import (
    ALL,
    DISPATCH_CAMPAIGN_FINISHED,
    DISPATCH_CAMPAIGN_STARTED,
    DISPATCH_STEP_COMPLETED,
    DISPATCH_STEP_STARTED,
    WORKER_FAILED,
    WORKER_FILE_CHANGED,
    WORKER_TOOL_STARTED,
    WORKER_TOOL_FINISHED,
    AuraEvent,
    EventBus,
    ALL_TOPICS,
    DISPATCH_TOPICS,
    WORKER_TOPICS,
)
from aura.bridge.worker_activity import (
    ActivityEntry,
    WorkerActivityController,
)


# ── AuraEvent tests ─────────────────────────────────────────────────────────


class TestAuraEvent:
    def test_defaults(self) -> None:
        ev = AuraEvent(topic="test.topic")
        assert ev.topic == "test.topic"
        assert ev.message == ""
        assert ev.payload == {}
        assert ev.source == ""
        assert ev.run_id == ""
        assert ev.campaign_id == ""
        assert ev.step_id == ""
        # timestamp should have been auto-stamped
        assert ev.timestamp > 0

    def test_explicit_timestamp_is_preserved(self) -> None:
        ts = 12345.0
        ev = AuraEvent(topic="t", timestamp=ts)
        assert ev.timestamp == ts

    def test_to_dict(self) -> None:
        ev = AuraEvent(
            topic="t",
            message="hello",
            payload={"key": "val"},
            source="test",
            run_id="r1",
            campaign_id="c1",
            step_id="s1",
            timestamp=0.0,
        )
        d = ev.to_dict()
        assert d["topic"] == "t"
        assert d["message"] == "hello"
        assert d["payload"] == {"key": "val"}
        assert d["source"] == "test"
        assert d["run_id"] == "r1"
        assert d["campaign_id"] == "c1"
        assert d["step_id"] == "s1"
        # timestamp was 0.0 so __post_init__ should have set it
        assert d["timestamp"] > 0

    def test_frozen(self) -> None:
        ev = AuraEvent(topic="t")
        with pytest.raises(AttributeError):
            ev.topic = "other"  # type: ignore[misc]

    def test_payload_copied(self) -> None:
        """to_dict returns a new dict, not a reference to internal state."""
        original: dict[str, Any] = {"a": [1]}
        ev = AuraEvent(topic="t", payload=original)
        d = ev.to_dict()
        original["a"] = [2]  # mutate original
        assert d["payload"]["a"] == [1]


# ── EventBus tests ──────────────────────────────────────────────────────────


class TestEventBus:
    """Core emit / subscribe / unsubscribe behaviour."""

    def test_emits_to_matching_subscribers(self) -> None:
        bus = EventBus()
        received: list[AuraEvent] = []

        bus.subscribe(DISPATCH_CAMPAIGN_STARTED, received.append)
        bus.subscribe(DISPATCH_STEP_STARTED, received.append)

        ev = AuraEvent(topic=DISPATCH_CAMPAIGN_STARTED)
        bus.emit(ev)

        assert received == [ev]

    def test_does_not_deliver_to_other_topics(self) -> None:
        bus = EventBus()
        received: list[AuraEvent] = []

        bus.subscribe(DISPATCH_CAMPAIGN_STARTED, received.append)
        bus.emit(AuraEvent(topic=WORKER_FAILED))

        assert received == []

    def test_preserves_event_order(self) -> None:
        bus = EventBus()
        order: list[int] = []

        bus.subscribe("order", lambda _: order.append(1))
        bus.subscribe("order", lambda _: order.append(2))
        bus.subscribe("order", lambda _: order.append(3))

        bus.emit(AuraEvent(topic="order"))
        assert order == [1, 2, 3]

    def test_unsubscribe_via_returned_callback(self) -> None:
        bus = EventBus()
        received: list[AuraEvent] = []

        unsub = bus.subscribe("t", received.append)
        ev = AuraEvent(topic="t")
        bus.emit(ev)
        assert len(received) == 1

        unsub()
        bus.emit(ev)
        assert len(received) == 1  # still 1 — second emit was not delivered

    def test_unsubscribe_direct(self) -> None:
        bus = EventBus()
        received: list[AuraEvent] = []

        bus.subscribe("t", received.append)
        bus.unsubscribe("t", received.append)
        bus.emit(AuraEvent(topic="t"))
        assert received == []  # subscribe then immediate unsubscribe → never fires

    def test_unsubscribe_noop_for_unknown_handler(self) -> None:
        bus = EventBus()
        bus.subscribe("t", lambda _: None)
        # removing a handler that was never added should not raise
        bus.unsubscribe("t", lambda _: None)
        assert bus.subscriber_count("t") == 1

    def test_multiple_emits_deliver_to_all(self) -> None:
        bus = EventBus()
        received: list[AuraEvent] = []

        bus.subscribe("t", received.append)

        e1 = AuraEvent(topic="t", message="first")
        e2 = AuraEvent(topic="t", message="second")
        bus.emit(e1)
        bus.emit(e2)

        assert received == [e1, e2]


class TestSubscriberIsolation:
    """One bad subscriber must not crash the bus for others."""

    def test_failing_handler_does_not_block_other_handlers(self) -> None:
        bus = EventBus()
        healthy: list[AuraEvent] = []

        def failing(_: AuraEvent) -> None:
            raise RuntimeError("boom")

        bus.subscribe("t", failing)
        bus.subscribe("t", healthy.append)

        bus.emit(AuraEvent(topic="t"))
        assert len(healthy) == 1

    def test_failing_wildcard_does_not_block_topic_subscribers(self) -> None:
        bus = EventBus()
        healthy: list[AuraEvent] = []

        def failing(_: AuraEvent) -> None:
            raise RuntimeError("boom")

        bus.subscribe(ALL, failing)
        bus.subscribe("t", healthy.append)

        bus.emit(AuraEvent(topic="t"))
        assert len(healthy) == 1


class TestWildcardSubscriber:
    """ALL (``*``) subscribers receive every event."""

    def test_wildcard_receives_all_events(self) -> None:
        bus = EventBus()
        received: list[AuraEvent] = []

        bus.subscribe(ALL, received.append)

        e1 = AuraEvent(topic="dispatch.campaign_started")
        e2 = AuraEvent(topic="worker.tool_started")
        bus.emit(e1)
        bus.emit(e2)

        assert received == [e1, e2]

    def test_wildcard_and_topic_subscriber_both_fire(self) -> None:
        bus = EventBus()
        wild: list[AuraEvent] = []
        topic: list[AuraEvent] = []

        bus.subscribe(ALL, wild.append)
        bus.subscribe(DISPATCH_CAMPAIGN_STARTED, topic.append)

        ev = AuraEvent(topic=DISPATCH_CAMPAIGN_STARTED)
        bus.emit(ev)

        assert wild == [ev]
        assert topic == [ev]

    def test_wildcard_does_not_fire_twice_for_same_topic(self) -> None:
        """When the same handler is registered for a topic and ALL,
        it should only be invoked once."""
        bus = EventBus()
        count: int = 0

        def counter(_: AuraEvent) -> None:
            nonlocal count
            count += 1

        bus.subscribe("t", counter)
        bus.subscribe(ALL, counter)

        bus.emit(AuraEvent(topic="t"))
        assert count == 1  # not 2


class TestBusLifecycle:
    """Clear and subscriber_count."""

    def test_subscriber_count(self) -> None:
        bus = EventBus()
        assert bus.subscriber_count() == 0

        bus.subscribe("a", lambda _: None)
        bus.subscribe("b", lambda _: None)
        assert bus.subscriber_count() == 2
        assert bus.subscriber_count("a") == 1
        assert bus.subscriber_count("b") == 1

    def test_clear_removes_all_subscribers(self) -> None:
        bus = EventBus()
        bus.subscribe("a", lambda _: None)
        bus.subscribe("b", lambda _: None)
        bus.clear()
        assert bus.subscriber_count() == 0


# ── Topic constant tests ────────────────────────────────────────────────────


class TestTopics:
    def test_all_topics_includes_dispatch_and_worker(self) -> None:
        assert DISPATCH_TOPICS | WORKER_TOPICS == ALL_TOPICS

    def test_topic_strings_are_dotted(self) -> None:
        for topic in ALL_TOPICS:
            assert "." in topic, f"{topic!r} is not a dotted topic string"

    def test_all_is_star(self) -> None:
        assert ALL == "*"


# ── Recorder helper (test-only, bounded) ────────────────────────────────────


@dataclass
class EventRecorder:
    """A bounded, in-memory event recorder for test use.

    Usage::

        recorder = EventRecorder()
        bus.subscribe(ALL, recorder.record)

        bus.emit(...)
        assert recorder.events[0].topic == "..."
    """

    events: list[AuraEvent] = field(default_factory=list)
    maxlen: int = 500

    def record(self, event: AuraEvent) -> None:
        if len(self.events) >= self.maxlen:
            return
        self.events.append(event)

    @property
    def count(self) -> int:
        return len(self.events)

    def clear(self) -> None:
        self.events.clear()

    def by_topic(self, topic: str) -> list[AuraEvent]:
        return [e for e in self.events if e.topic == topic]


class TestEventRecorder:
    def test_records_events(self) -> None:
        bus = EventBus()
        recorder = EventRecorder()
        bus.subscribe(ALL, recorder.record)

        bus.emit(AuraEvent(topic="a"))
        bus.emit(AuraEvent(topic="b"))

        assert recorder.count == 2
        assert recorder.by_topic("a")[0].topic == "a"

    def test_respects_maxlen(self) -> None:
        recorder = EventRecorder(maxlen=3)
        for i in range(5):
            recorder.record(AuraEvent(topic=f"t{i}"))
        assert recorder.count == 3

    def test_clear(self) -> None:
        recorder = EventRecorder()
        recorder.record(AuraEvent(topic="a"))
        recorder.clear()
        assert recorder.count == 0


# ── WorkerActivityController tests ───────────────────────────────────────────


class TestWorkerActivityController:
    """WorkerActivityController consumes the event bus and projects entries."""

    def test_receives_tool_events(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "read_file"}))
        bus.emit(AuraEvent(topic=WORKER_TOOL_FINISHED, payload={"name": "read_file", "ok": True}))

        entries = ctrl.snapshot()
        assert len(entries) == 2
        assert entries[0].kind == "tool_started"
        assert "read_file" in entries[0].message
        assert entries[1].kind == "tool_completed"

    def test_preserves_order(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "a"}))
        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "b"}))
        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "c"}))

        entries = ctrl.snapshot()
        names = [e.detail for e in entries]
        assert names == ["a", "b", "c"]

    def test_caps_history(self) -> None:
        ctrl = WorkerActivityController(EventBus(), maxlen=3)

        # Feed through _append so the cap logic fires
        for i in range(5):
            ctrl._append(ActivityEntry(kind="t", message=f"entry {i}"))

        snapshot = ctrl.snapshot()
        assert snapshot[-1].message == "entry 4"
        assert len(snapshot) <= 3

    def test_snapshot_returns_copy(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "x"}))
        snap1 = ctrl.snapshot()
        snap2 = ctrl.snapshot()
        assert snap1 is not snap2  # different list objects

    def test_snapshot_dicts(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "x"}))
        dicts = ctrl.snapshot_dicts()
        assert isinstance(dicts[0], dict)
        assert dicts[0]["kind"] == "tool_started"

    def test_on_change_callback(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        snapshots: list[list] = []
        ctrl.set_on_change(lambda s: snapshots.append(s))

        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "a"}))
        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "b"}))

        assert len(snapshots) == 2
        assert snapshots[1][-1].detail == "b"

    def test_file_changed_event(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_FILE_CHANGED, payload={
            "path": "src/main.py",
            "action": "created",
        }))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "file_changed"
        assert "src/main.py" in entries[0].message

    def test_worker_failed_event(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_FAILED, payload={
            "error": "connection timeout",
        }))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "worker_failed"
        assert "timeout" in entries[0].message

    def test_clear(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "x"}))
        assert len(ctrl.snapshot()) == 1
        ctrl.clear()
        assert len(ctrl.snapshot()) == 0

    def test_activity_events_are_self_contained(self) -> None:
        """Activity events are recorded without depending on other projectors."""
        bus = EventBus()
        ctrl = WorkerActivityController(bus)
        bus.emit(AuraEvent(topic=WORKER_TOOL_STARTED, payload={"name": "read_file"}))
        bus.emit(AuraEvent(topic=WORKER_FAILED, payload={}))
        entries = ctrl.snapshot()
        assert all(hasattr(e, "kind") for e in entries)

    def test_campaign_started_event(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=DISPATCH_CAMPAIGN_STARTED, payload={
            "goal": "Fix the auth bug",
            "step_count": 3,
        }))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "campaign_started"
        assert "Fix the auth bug" in entries[0].message

    def test_step_started_event(self) -> None:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        bus.emit(AuraEvent(topic=DISPATCH_STEP_STARTED, payload={
            "step_id": "step-1",
            "description": "Add tests for auth",
        }))

        entries = ctrl.snapshot()
        assert len(entries) == 1
        assert entries[0].kind == "step_started"
        assert "Add tests for auth" in entries[0].message
