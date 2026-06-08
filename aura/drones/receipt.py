"""Receipt produced after a Drone execution completes."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DroneReceipt:
    """Summary of a completed Drone run."""
    run_id: str
    drone_id: str
    drone_name: str
    status: str  # completed, failed, cancelled, timed_out
    started_at: str  # ISO format
    ended_at: str    # ISO format
    tool_calls_made: int = 0
    tool_errors: int = 0
    summary: str = ""
    output_contract: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        """Serializable dict for JSON persistence."""
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DroneReceipt:
        return cls(**data)
