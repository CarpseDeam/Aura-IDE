"""Tests for the lifecycle hooks package (aura.lifecycle)."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from aura.events import ALL, EventBus
from aura.events.event import AuraEvent
from aura.lifecycle import (
    GateDecision,
    GateHookRegistry,
    HandlerRecord,
    HookContext,
    HookMatcher,
    LifecycleHooks,
    NotifyHookRegistry,
    attach_lifecycle_notify,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_ctx(
    *,
    topic: str = "test.topic",
    category: str = "notify",
    phase: str = "",
    role: str = "",
    tool_name: str = "",
    **overrides: Any,
) -> HookContext:
    return HookContext(
        topic=topic,
        category=category,
        phase=phase,
        role=role,
        tool_name=tool_name,
        **overrides,
    )


def _run_async(coro: object) -> Any:
    """Run an async function to completion synchronously."""
    return asyncio.run(coro)  # type: ignore[arg-type]


# ── HookContext ──────────────────────────────────────────────────────────────


class TestHookContext:
    """HookContext creation and from_event factory."""

    def test_defaults(self) -> None:
        ctx = HookContext(topic="t", category="notify")
        assert ctx.topic == "t"
        assert ctx.category == "notify"
        assert ctx.phase == ""
        assert ctx.role == ""
        assert ctx.run_id == ""
        assert ctx.artifact_id == ""
        assert ctx.artifact_item_id == ""
        assert ctx.tool_call_id == ""
        assert ctx.parent_tool_call_id == ""
        assert ctx.tool_name == ""
        assert ctx.payload == {}
        assert ctx.metadata == {}

    def test_field_assignment_raises_frozen_instance_error(self) -> None:
        ctx = HookContext(topic="t", category="notify")
        with pytest.raises(FrozenInstanceError):
            ctx.topic = "other"

    def test_from_event_copies_identity(self) -> None:
        ev = AuraEvent(
            topic="worker.tool.started",
            payload={"key": "val"},
            run_id="r1",
            artifact_id="art-1",
            artifact_item_id="item-1",
        )
        ctx = HookContext.from_event(ev)
        assert ctx.topic == "worker.tool.started"
        assert ctx.run_id == "r1"
        assert ctx.artifact_id == "art-1"
        assert ctx.artifact_item_id == "item-1"
        assert ctx.payload == {"key": "val"}
        assert ctx.category == "notify"  # default

    def test_constructor_copies_payload(self) -> None:
        payload = {"key": "val"}
        ctx = HookContext(topic="t", category="notify", payload=payload)

        payload["key"] = "changed"

        assert ctx.payload == {"key": "val"}

    def test_from_event_accepts_overrides(self) -> None:
        ev = AuraEvent(topic="t", run_id="r1")
        ctx = HookContext.from_event(
            ev,
            category="gate",
            phase="pre_tool",
            role="worker",
            tool_name="write_file",
        )
        assert ctx.category == "gate"
        assert ctx.phase == "pre_tool"
        assert ctx.role == "worker"
        assert ctx.tool_name == "write_file"

    def test_to_dict_includes_all_fields(self) -> None:
        ctx = HookContext(
            topic="t",
            category="notify",
            run_id="r1",
            payload={"a": 1},
        )
        d = ctx.to_dict()
        assert d["topic"] == "t"
        assert d["category"] == "notify"
        assert d["run_id"] == "r1"
        assert d["payload"] == {"a": 1}


# ── HookMatcher ─────────────────────────────────────────────────────────────


class TestHookMatcher:
    """HookMatcher matching behaviour."""

    def test_exact_topic_match(self) -> None:
        m = HookMatcher("worker.tool.started")
        ctx = _make_ctx(topic="worker.tool.started")
        assert m.matches(ctx)

    def test_field_assignment_raises_frozen_instance_error(self) -> None:
        m = HookMatcher("*")
        with pytest.raises(FrozenInstanceError):
            m.topic = "other"

    def test_exact_topic_no_match(self) -> None:
        m = HookMatcher("worker.tool.started")
        ctx = _make_ctx(topic="worker.tool.finished")
        assert not m.matches(ctx)

    def test_wildcard_matches_everything(self) -> None:
        m = HookMatcher("*")
        assert m.matches(_make_ctx(topic="anything"))
        assert m.matches(_make_ctx(topic="worker.tool.started"))

    def test_phase_filter(self) -> None:
        m = HookMatcher("*", phase="pre_tool")
        assert m.matches(_make_ctx(phase="pre_tool"))
        assert not m.matches(_make_ctx(phase="post_tool"))

    def test_role_filter(self) -> None:
        m = HookMatcher("*", role="worker")
        assert m.matches(_make_ctx(role="worker"))
        assert not m.matches(_make_ctx(role="planner"))

    def test_tool_name_filter(self) -> None:
        m = HookMatcher("*", tool_name="write_file")
        assert m.matches(_make_ctx(tool_name="write_file"))
        assert not m.matches(_make_ctx(tool_name="execute_bash"))

    def test_combined_filters_all_match(self) -> None:
        m = HookMatcher(
            "worker.tool.started",
            phase="pre_tool",
            role="worker",
            tool_name="write_file",
        )
        ctx = _make_ctx(
            topic="worker.tool.started",
            phase="pre_tool",
            role="worker",
            tool_name="write_file",
        )
        assert m.matches(ctx)

    def test_combined_filters_one_mismatch(self) -> None:
        m = HookMatcher(
            "worker.tool.started",
            phase="pre_tool",
            role="worker",
            tool_name="write_file",
        )
        ctx = _make_ctx(
            topic="worker.tool.started",
            phase="post_tool",  # mismatch
            role="worker",
            tool_name="write_file",
        )
        assert not m.matches(ctx)

    def test_to_dict(self) -> None:
        m = HookMatcher("t", phase="p", role="r", tool_name="tn")
        d = m.to_dict()
        assert d == {"topic": "t", "phase": "p", "role": "r", "tool_name": "tn"}
        empty = HookMatcher("*")
        assert empty.to_dict()["topic"] == "*"


# ── HandlerRecord ───────────────────────────────────────────────────────────


class TestHandlerRecord:
    """HandlerRecord carries source/kind metadata for future extension."""

    def test_defaults(self) -> None:
        matcher = HookMatcher("*")
        rec = HandlerRecord(name="test", matcher=matcher, callback=lambda ctx: None)
        assert rec.name == "test"
        assert rec.handler_kind == "python"
        assert rec.source == "internal"
        assert rec.metadata == {}

    def test_field_assignment_raises_frozen_instance_error(self) -> None:
        matcher = HookMatcher("*")
        rec = HandlerRecord(name="test", matcher=matcher, callback=lambda ctx: None)
        with pytest.raises(FrozenInstanceError):
            rec.name = "other"

    def test_explicit_source_and_kind(self) -> None:
        matcher = HookMatcher("*")
        rec = HandlerRecord(
            name="user_hook",
            matcher=matcher,
            callback=lambda ctx: None,
            handler_kind="command",
            source="user",
            metadata={"path": "/hooks/hello.sh"},
        )
        assert rec.handler_kind == "command"
        assert rec.source == "user"
        assert rec.metadata == {"path": "/hooks/hello.sh"}


# ── NotifyHookRegistry ──────────────────────────────────────────────────────


class TestNotifyHookRegistry:
    """Notify hook registry behaviour."""

    def test_exact_topic_match(self) -> None:
        reg = NotifyHookRegistry()
        received: list[HookContext] = []

        reg.register(HookMatcher("t.a"), received.append)
        reg.notify(_make_ctx(topic="t.a"))

        assert len(received) == 1
        assert received[0].topic == "t.a"

    def test_wildcard_match(self) -> None:
        reg = NotifyHookRegistry()
        received: list[HookContext] = []

        reg.register(HookMatcher("*"), received.append)
        reg.notify(_make_ctx(topic="anything"))

        assert len(received) == 1

    def test_no_match_no_fire(self) -> None:
        reg = NotifyHookRegistry()
        received: list[HookContext] = []

        reg.register(HookMatcher("t.a"), received.append)
        reg.notify(_make_ctx(topic="t.b"))

        assert len(received) == 0

    def test_return_value_ignored(self) -> None:
        reg = NotifyHookRegistry()
        received: list[HookContext] = []

        reg.register(HookMatcher("*"), lambda ctx: "ignored")
        reg.register(HookMatcher("*"), received.append)

        reg.notify(_make_ctx())
        # Both should fire — the first handler's return is silently discarded.
        assert len(received) == 1

    def test_exception_isolated_next_handler_still_runs(self) -> None:
        reg = NotifyHookRegistry()
        received: list[HookContext] = []

        def failing(_ctx: HookContext) -> None:
            raise RuntimeError("boom")

        reg.register(HookMatcher("*"), failing)
        reg.register(HookMatcher("*"), received.append)

        reg.notify(_make_ctx())
        # The second handler should still fire after the first raises.
        assert len(received) == 1

    def test_unsubscribe_removes_handler(self) -> None:
        reg = NotifyHookRegistry()
        received: list[HookContext] = []

        unsub = reg.register(HookMatcher("*"), received.append)
        reg.notify(_make_ctx())
        assert len(received) == 1

        unsub()
        reg.notify(_make_ctx())
        # Handler should not fire again after unsubscribe.
        assert len(received) == 1

    def test_handler_order_is_registration_order(self) -> None:
        reg = NotifyHookRegistry()
        order: list[str] = []

        reg.register(HookMatcher("*"), lambda _: order.append("first"))
        reg.register(HookMatcher("*"), lambda _: order.append("second"))

        reg.notify(_make_ctx())
        assert order == ["first", "second"]


# ── GateDecision ────────────────────────────────────────────────────────────


class TestGateDecision:
    """GateDecision constructors."""

    def test_allow_default(self) -> None:
        d = GateDecision.allow()
        assert d.allowed
        assert not d.blocked
        assert d.reason == ""

    def test_field_assignment_raises_frozen_instance_error(self) -> None:
        d = GateDecision.allow()
        with pytest.raises(FrozenInstanceError):
            d.allowed = False

    def test_block(self) -> None:
        d = GateDecision.block("not allowed", severity="warning")
        assert not d.allowed
        assert d.blocked
        assert d.reason == "not allowed"
        assert d.severity == "warning"

    def test_rewrite(self) -> None:
        d = GateDecision.rewrite({"new": "payload"}, reason="transformed")
        assert d.allowed
        assert not d.blocked
        assert d.updated_payload == {"new": "payload"}
        assert d.reason == "transformed"

    def test_rewrite_copies_updated_payload(self) -> None:
        updated_payload = {"new": "payload"}
        d = GateDecision.rewrite(updated_payload, reason="transformed")

        updated_payload["new"] = "changed"

        assert d.updated_payload == {"new": "payload"}

    def test_inject_context(self) -> None:
        d = GateDecision.inject_context("extra info", reason="enrich")
        assert d.allowed
        assert not d.blocked
        assert d.additional_context == "extra info"
        assert d.reason == "enrich"

    def test_force_continuation(self) -> None:
        d = GateDecision.force_continuation("keep going")
        assert d.allowed
        assert not d.blocked
        assert d.force_continue
        assert d.reason == "keep going"


# ── GateHookRegistry ────────────────────────────────────────────────────────


class TestGateHookRegistry:
    """Gate hook registry behaviour."""

    def test_default_allows_with_no_handlers(self) -> None:
        reg = GateHookRegistry()
        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.allowed
        assert not decision.blocked

    def test_block_wins(self) -> None:
        reg = GateHookRegistry()
        reg.register(HookMatcher("*"), lambda ctx: GateDecision.block("stop"))
        reg.register(HookMatcher("*"), lambda ctx: GateDecision.allow())

        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.blocked
        assert "stop" in decision.reason

    def test_inject_context_merges(self) -> None:
        reg = GateHookRegistry()
        reg.register(
            HookMatcher("*"),
            lambda ctx: GateDecision.inject_context("ctx-a"),
        )
        reg.register(
            HookMatcher("*"),
            lambda ctx: GateDecision.inject_context("ctx-b"),
        )

        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.allowed
        assert "ctx-a" in decision.additional_context
        assert "ctx-b" in decision.additional_context

    def test_one_rewrite_works(self) -> None:
        reg = GateHookRegistry()
        reg.register(
            HookMatcher("*"),
            lambda ctx: GateDecision.rewrite({"transformed": True}),
        )

        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.allowed
        assert decision.updated_payload == {"transformed": True}

    def test_multiple_rewrites_blocked(self) -> None:
        reg = GateHookRegistry()
        reg.register(
            HookMatcher("*"),
            lambda ctx: GateDecision.rewrite({"a": 1}),
        )
        reg.register(
            HookMatcher("*"),
            lambda ctx: GateDecision.rewrite({"b": 2}),
        )

        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.blocked
        assert decision.reason == "lifecycle_gate_multiple_rewriters"

    def test_matcher_filters_by_tool_name(self) -> None:
        reg = GateHookRegistry()
        reg.register(
            HookMatcher("*", tool_name="write_file"),
            lambda ctx: GateDecision.block("file writes blocked"),
        )

        blocked = _run_async(reg.ask(_make_ctx(tool_name="write_file")))
        assert blocked.blocked

        allowed = _run_async(reg.ask(_make_ctx(tool_name="execute_bash")))
        assert allowed.allowed

    def test_handler_exception_returns_blocked(self) -> None:
        reg = GateHookRegistry()

        def failing(_ctx: HookContext) -> GateDecision:
            raise RuntimeError("oh no")

        reg.register(HookMatcher("*"), failing)

        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.blocked
        assert decision.reason == "lifecycle_gate_handler_error"

    def test_async_handler_supported(self) -> None:
        reg = GateHookRegistry()

        async def async_handler(_ctx: HookContext) -> GateDecision:
            await asyncio.sleep(0)
            return GateDecision.inject_context("async-context")

        reg.register(HookMatcher("*"), async_handler)

        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.allowed
        assert decision.additional_context == "async-context"

    def test_invalid_handler_return_blocks(self) -> None:
        reg = GateHookRegistry()
        reg.register(HookMatcher("*"), lambda ctx: "invalid")

        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.blocked
        assert decision.reason == "lifecycle_gate_invalid_decision"

    def test_force_continue_preserved(self) -> None:
        reg = GateHookRegistry()
        reg.register(
            HookMatcher("*"),
            lambda ctx: GateDecision.force_continuation("keep going"),
        )

        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.allowed
        assert decision.force_continue

    def test_unsubscribe_removes_handler(self) -> None:
        reg = GateHookRegistry()
        unsub = reg.register(
            HookMatcher("*"),
            lambda ctx: GateDecision.block("should not fire"),
        )
        unsub()
        decision = _run_async(reg.ask(_make_ctx()))
        assert decision.allowed


# ── LifecycleHooks facade ────────────────────────────────────────────────────


class TestLifecycleHooksFacade:
    """LifecycleHooks top-level facade."""

    def test_register_notify_and_notify(self) -> None:
        lh = LifecycleHooks()
        received: list[HookContext] = []

        lh.register_notify(HookMatcher("*"), received.append)
        lh.notify(_make_ctx())
        assert len(received) == 1

    def test_register_gate_and_ask(self) -> None:
        lh = LifecycleHooks()
        lh.register_gate(
            HookMatcher("*"),
            lambda ctx: GateDecision.block("nope"),
        )

        decision = _run_async(lh.ask(_make_ctx()))
        assert decision.blocked

    def test_gate_returns_allow_when_no_handlers(self) -> None:
        lh = LifecycleHooks()
        decision = _run_async(lh.ask(_make_ctx()))
        assert decision.allowed


# ── Event Adapter ─────────────────────────────────────────────────────────


class TestAttachLifecycleNotify:
    """EventBus → LifecycleHooks adapter behaviour."""

    def test_event_bus_emit_reaches_notify_handler(self) -> None:
        bus = EventBus()
        lh = LifecycleHooks()
        received: list[HookContext] = []

        lh.register_notify(HookMatcher("*"), received.append)
        unsub = attach_lifecycle_notify(bus, lh)

        bus.emit(AuraEvent(topic="worker.tool_started", run_id="r1"))
        assert len(received) == 1
        assert received[0].topic == "worker.tool_started"
        assert received[0].run_id == "r1"
        assert received[0].category == "notify"

        unsub()

    def test_adapter_returns_unsubscribe_callable(self) -> None:
        bus = EventBus()
        lh = LifecycleHooks()

        unsub = attach_lifecycle_notify(bus, lh)
        assert callable(unsub)

        # Should be callable without error.
        unsub()

    def test_after_unsubscribe_notify_no_longer_receives(self) -> None:
        bus = EventBus()
        lh = LifecycleHooks()
        received: list[HookContext] = []

        lh.register_notify(HookMatcher("*"), received.append)
        unsub = attach_lifecycle_notify(bus, lh)

        bus.emit(AuraEvent(topic="t1"))
        assert len(received) == 1

        unsub()
        bus.emit(AuraEvent(topic="t2"))
        # No further events should arrive after detach.
        assert len(received) == 1

    def test_existing_subscribers_still_work_alongside_adapter(self) -> None:
        bus = EventBus()
        lh = LifecycleHooks()
        direct: list[AuraEvent] = []
        lifecycle: list[HookContext] = []

        bus.subscribe(ALL, direct.append)
        lh.register_notify(HookMatcher("*"), lifecycle.append)
        unsub = attach_lifecycle_notify(bus, lh)

        ev = AuraEvent(topic="work_artifact.item_ready", artifact_id="art-1", artifact_item_id="item-1")
        bus.emit(ev)

        assert len(direct) == 1
        assert direct[0] is ev
        assert len(lifecycle) == 1
        assert lifecycle[0].topic == "work_artifact.item_ready"

        unsub()

    def test_adapter_uses_all_wildcard_subscription(self) -> None:
        """Lifecycle notify should receive events of any topic."""
        bus = EventBus()
        lh = LifecycleHooks()
        received: list[HookContext] = []

        lh.register_notify(HookMatcher("*"), received.append)
        unsub = attach_lifecycle_notify(bus, lh)

        bus.emit(AuraEvent(topic="dispatch.campaign_started"))
        bus.emit(AuraEvent(topic="worker.tool_finished"))
        bus.emit(AuraEvent(topic="worker.file_changed"))

        assert len(received) == 3
        assert [c.topic for c in received] == [
            "dispatch.campaign_started",
            "worker.tool_finished",
            "worker.file_changed",
        ]

        unsub()
