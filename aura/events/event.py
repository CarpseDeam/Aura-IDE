"""The AuraEvent dataclass — one immutable fact on the event bus."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AuraEvent:
    """An immutable fact published to the event bus.

    Every event carries a ``topic`` string that subscribers match against.
    Optional fields carry identity context (run, artifact) so that
    downstream projectors can correlate events without reaching into
    other subsystems.
    """

    topic: str
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    timestamp: float = 0.0
    run_id: str = ""
    artifact_id: str = ""
    artifact_item_id: str = ""

    def __post_init__(self) -> None:
        """Stamp *timestamp* with the current monotonic time if left at 0."""
        if self.timestamp == 0.0:
            # Frozen dataclass → object.__setattr__ to work around immutability.
            object.__setattr__(self, "timestamp", time.time())

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict for serialisation or logging."""
        return {
            "topic": self.topic,
            "message": self.message,
            "payload": dict(self.payload),
            "source": self.source,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "artifact_id": self.artifact_id,
            "artifact_item_id": self.artifact_item_id,
        }
