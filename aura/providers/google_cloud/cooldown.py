import threading
import time


class CooldownManager:
    """Thread-safe rate-limit cooldown timer."""

    def __init__(self, cooldown_seconds: float = 30.0) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._until: float = 0.0
        self._lock = threading.Lock()

    def hit(self) -> None:
        """Record a rate-limit hit; start the cooldown window."""
        with self._lock:
            self._until = time.monotonic() + self._cooldown_seconds

    def is_cooling(self) -> bool:
        """Return True if we are currently in a cooldown window."""
        with self._lock:
            return time.monotonic() < self._until

    def remaining(self) -> float:
        """Return the number of seconds remaining in the cooldown, or 0.0."""
        with self._lock:
            return max(0.0, self._until - time.monotonic())

    def reset(self) -> None:
        """Clear any active cooldown."""
        with self._lock:
            self._until = 0.0
