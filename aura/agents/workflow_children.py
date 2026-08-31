"""Invocation-scoped child collaborators for foreground workflow workers."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator


class WorkflowChildSource:
    """Produce a distinct child collaborator or serialize an unsafe injection.

    ``ChildExecutor`` exposes ``fork()`` and therefore gives every active Step
    and helper its own collaborator.  Older test/application injections remain
    supported: when they cannot fork, identity reuse is serialized. A nested
    attempt to reuse that same mutable collaborator fails closed rather than
    deadlocking or exchanging helper context.
    """

    def __init__(self, prototype: Any) -> None:
        self._prototype = prototype
        self._condition = threading.Condition()
        self._fork_lock = threading.Lock()
        self._active: dict[int, tuple[Any, int]] = {}

    @contextmanager
    def invocation(self, *, fail_if_active: bool = False) -> Iterator[Any]:
        fork = getattr(self._prototype, "fork", None)
        with self._fork_lock:
            child = fork() if callable(fork) else self._prototype
        owner = threading.get_ident()
        key = id(child)
        with self._condition:
            while key in self._active:
                if fail_if_active or self._active[key][1] == owner:
                    raise RuntimeError(
                        "An injected workflow child collaborator was reused by a "
                        "nested invocation and cannot be isolated."
                    )
                self._condition.wait()
            self._active[key] = (child, owner)
        try:
            yield child
        finally:
            with self._condition:
                self._active.pop(key, None)
                self._condition.notify_all()


__all__ = ["WorkflowChildSource"]
