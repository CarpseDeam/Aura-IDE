"""Central hook/event system for registering and triggering agent backends."""

from __future__ import annotations

from typing import Any, Callable


class HookManager:
    """A simple pub-sub hook registry.

    Hooks are identified by a string name. Each hook can have at most one
    registered handler. The trigger method calls the handler with the given
    keyword arguments and returns its result.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}

    def register(self, name: str, handler: Callable) -> None:
        """Register a callable for the given hook name.

        Args:
            name: Hook identifier (e.g. 'generate_worker_code').
            handler: A callable that will receive **kwargs from trigger().

        Raises:
            ValueError: If a handler is already registered for this name.
        """
        if name in self._handlers:
            raise ValueError(f"Hook '{name}' already has a registered handler.")
        self._handlers[name] = handler

    def unregister(self, name: str) -> None:
        """Remove the handler for a hook."""
        self._handlers.pop(name, None)

    def trigger(self, name: str, **kwargs: Any) -> Any:
        """Call the registered handler for *name* with the given keyword args.

        Args:
            name: Hook identifier.
            **kwargs: Arguments forwarded to the registered handler.

        Returns:
            The return value of the handler.

        Raises:
            RuntimeError: If no handler is registered for *name*.
        """
        handler = self._handlers.get(name)
        if handler is None:
            raise RuntimeError(
                f"No handler registered for hook '{name}'. "
                f"Registered hooks: {list(self._handlers)}"
            )
        return handler(**kwargs)

    def is_registered(self, name: str) -> bool:
        """Return True if a handler is registered for *name*."""
        return name in self._handlers


# Module-level singleton — import this everywhere.
hooks = HookManager()
