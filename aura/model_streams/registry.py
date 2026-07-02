"""Model stream registry for registering and triggering agent backends.

This provides the central pub-sub registry that maps named model-generation
streams (e.g. ``generate_planner_code``, ``generate_worker_code``) to their
backend handlers.  Each stream name can have at most one registered handler.
"""

from __future__ import annotations

from typing import Any, Callable


class ModelStreamRegistry:
    """A pub-sub registry for model backend streams.

    Streams are identified by a string name.  Each stream can have at most one
    registered handler.  The :meth:`trigger` method calls the handler with the
    given keyword arguments and returns its result.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, handler: Callable) -> None:
        """Register a callable for the given stream name.

        Args:
            name: Stream identifier (e.g. ``'generate_worker_code'``).
            handler: A callable that will receive ``**kwargs`` from
                :meth:`trigger`.

        Raises:
            ValueError: If a handler is already registered for this name.
        """
        if name in self._handlers:
            raise ValueError(
                f"Stream '{name}' already has a registered handler."
            )
        self._handlers[name] = handler

    def unregister(self, name: str) -> None:
        """Remove the handler for a stream."""
        self._handlers.pop(name, None)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_handler(self, name: str) -> Callable | None:
        """Return the handler registered for *name*, or ``None``."""
        return self._handlers.get(name)

    def is_registered(self, name: str) -> bool:
        """Return ``True`` if a handler is registered for *name*."""
        return name in self._handlers

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------

    def trigger(self, name: str, **kwargs: Any) -> Any:
        """Call the registered handler for *name* with the given keyword args.

        Args:
            name: Stream identifier.
            **kwargs: Arguments forwarded to the registered handler.

        Returns:
            The return value of the handler.

        Raises:
            RuntimeError: If no handler is registered for *name*.
        """
        handler = self._handlers.get(name)
        if handler is None:
            raise RuntimeError(
                f"No handler registered for stream '{name}'. "
                f"Registered streams: {list(self._handlers)}"
            )
        return handler(**kwargs)


# Module-level singleton — import this everywhere.
model_streams = ModelStreamRegistry()
