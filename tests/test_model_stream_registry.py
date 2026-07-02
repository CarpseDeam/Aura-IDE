"""Tests for the model stream registry (aura.model_streams)."""

from __future__ import annotations

import pytest

from aura.model_streams import ModelStreamRegistry, model_streams


class TestModelStreamRegistry:
    """Behaviour of the ModelStreamRegistry class."""

    def test_register_and_trigger_returns_handler_output(self) -> None:
        registry = ModelStreamRegistry()

        def handler(**kwargs: object) -> str:
            return f"got: {kwargs.get('value')}"

        registry.register("test_stream", handler)
        result = registry.trigger("test_stream", value=42)
        assert result == "got: 42"

    def test_duplicate_register_raises_value_error(self) -> None:
        registry = ModelStreamRegistry()
        registry.register("test_stream", lambda **kw: None)
        with pytest.raises(ValueError):
            registry.register("test_stream", lambda **kw: None)

    def test_unregister_removes_handler(self) -> None:
        registry = ModelStreamRegistry()

        def handler(**kwargs: object) -> str:
            return "called"

        registry.register("test_stream", handler)
        assert registry.is_registered("test_stream")

        registry.unregister("test_stream")
        assert not registry.is_registered("test_stream")

    def test_trigger_unregistered_raises_runtime_error(self) -> None:
        registry = ModelStreamRegistry()
        with pytest.raises(RuntimeError):
            registry.trigger("nonexistent")

    def test_get_handler_returns_none_for_unregistered(self) -> None:
        registry = ModelStreamRegistry()
        assert registry.get_handler("nonexistent") is None

    def test_get_handler_returns_handler_when_registered(self) -> None:
        registry = ModelStreamRegistry()

        def handler(**kwargs: object) -> str:
            return "ok"

        registry.register("test_stream", handler)
        assert registry.get_handler("test_stream") is handler


class TestModelStreamsSingleton:
    """The module-level singleton exported from aura.model_streams."""

    def test_singleton_is_model_stream_registry(self) -> None:
        assert isinstance(model_streams, ModelStreamRegistry)

    def test_singleton_works_end_to_end(self) -> None:
        called: list[str] = []

        def handler(**kwargs: object) -> str:
            called.append(kwargs.get("msg", ""))
            return "done"

        model_streams.register("_test_singleton", handler)
        try:
            result = model_streams.trigger("_test_singleton", msg="hello")
            assert result == "done"
            assert called == ["hello"]
        finally:
            model_streams.unregister("_test_singleton")
