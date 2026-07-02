"""Tests for EventBus notify contract rules.

These tests probe specific behavioural contracts of the EventBus that are
important for correctness of the dispatch lifecycle but are easy to regress:

1. Topic-specific subscribers fire before wildcard subscribers, regardless
   of registration order.
2. emit() returns None and never blocks on handler return values.
3. A handler registered for both a specific topic and ``ALL`` fires only
   once for a matching event.
"""

from __future__ import annotations

from aura.events import ALL, AuraEvent, EventBus


class TestOrderingContract:
    """Topic-specific subscribers must fire before wildcard subscribers."""

    def test_topic_specific_fires_before_wildcard(self) -> None:
        bus = EventBus()
        order: list[str] = []

        # Register wildcard *first*, topic-specific *second*.
        bus.subscribe(ALL, lambda _: order.append("wildcard"))
        bus.subscribe("topic.a", lambda _: order.append("topic.a"))

        bus.emit(AuraEvent(topic="topic.a"))

        # Topic-specific fires first even though it was registered second.
        assert order == ["topic.a", "wildcard"], (
            f"Expected topic-specific first, got {order}"
        )

    def test_topic_specific_fires_first_with_several_wildcards(self) -> None:
        bus = EventBus()
        order: list[int] = []

        bus.subscribe(ALL, lambda _: order.append(1))
        bus.subscribe("t", lambda _: order.append(2))
        bus.subscribe(ALL, lambda _: order.append(3))

        bus.emit(AuraEvent(topic="t"))

        # Topic-specific (2) fires before both wildcards (1, 3),
        # but 1 registered first among wildcards, then 3.
        assert order == [2, 1, 3], f"Unexpected order: {order}"

    def test_wildcard_not_fired_when_handler_already_fired_for_topic(self) -> None:
        """A handler shared between topic and ALL must not double-fire."""
        bus = EventBus()
        count: int = 0

        def handler(_: AuraEvent) -> None:
            nonlocal count
            count += 1

        bus.subscribe("t", handler)
        bus.subscribe(ALL, handler)

        bus.emit(AuraEvent(topic="t"))
        assert count == 1


class TestEmitReturnContract:
    """emit() returns None and ignores handler return values."""

    def test_emit_returns_none(self) -> None:
        bus = EventBus()
        bus.subscribe("t", lambda _: "unused")
        result = bus.emit(AuraEvent(topic="t"))
        assert result is None, f"Expected None, got {result!r}"

    def test_handler_returning_false_does_not_block_others(self) -> None:
        bus = EventBus()
        received: list[AuraEvent] = []

        bus.subscribe("t", lambda _: False)
        bus.subscribe("t", received.append)

        ev = AuraEvent(topic="t")
        bus.emit(ev)

        assert received == [ev], "Handler returning False blocked delivery"

    def test_handler_raising_does_not_block_returning_none(self) -> None:
        """Even a raising handler does not change that emit() returns None."""
        bus = EventBus()

        def failing(_: AuraEvent) -> None:
            raise RuntimeError("boom")

        bus.subscribe("t", failing)
        result = bus.emit(AuraEvent(topic="t"))
        assert result is None

    def test_multiple_return_values_all_ignored(self) -> None:
        bus = EventBus()
        bus.subscribe("t", lambda _: False)
        bus.subscribe("t", lambda _: 42)
        bus.subscribe("t", lambda _: "keep-going")

        result = bus.emit(AuraEvent(topic="t"))
        assert result is None


class TestSingleFireContract:
    """A handler registered for a topic *and* ALL fires only once."""

    def test_handler_shared_between_topic_and_all_fires_once(self) -> None:
        bus = EventBus()
        fired: list[str] = []

        def handler(ev: AuraEvent) -> None:
            fired.append(ev.topic)

        bus.subscribe("my.topic", handler)
        bus.subscribe(ALL, handler)

        bus.emit(AuraEvent(topic="my.topic"))
        assert fired == ["my.topic"], f"Fired {len(fired)} times: {fired}"

    def test_multiple_shared_handlers(self) -> None:
        bus = EventBus()
        results: list[int] = []

        def h1(_: AuraEvent) -> None:
            results.append(1)

        def h2(_: AuraEvent) -> None:
            results.append(2)

        # h1 is registered for both topic and ALL; h2 only for topic.
        bus.subscribe("t", h1)
        bus.subscribe(ALL, h1)
        bus.subscribe("t", h2)

        bus.emit(AuraEvent(topic="t"))
        # h1 fires only once (topic path), h2 fires once (topic path).
        assert results == [1, 2], f"Unexpected: {results}"

    def test_two_wildcards_and_one_topic_handler_all_separate(self) -> None:
        """Three distinct handlers: no dedup needed, all fire."""
        bus = EventBus()
        order: list[str] = []

        bus.subscribe("t", lambda _: order.append("topic"))
        bus.subscribe(ALL, lambda _: order.append("wild-1"))
        bus.subscribe(ALL, lambda _: order.append("wild-2"))

        bus.emit(AuraEvent(topic="t"))
        assert order == ["topic", "wild-1", "wild-2"]
