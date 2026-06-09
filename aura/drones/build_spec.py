from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DroneBuildBrief:
    """Lightweight build brief produced by the Drone Workshop."""

    response_type: str  # "question" | "brief"
    message: str  # natural-language message to show the user
    ready_to_build: bool = False  # True when Workshop has enough info
    build_brief: str = ""  # plain-language brief for Planner/Worker

    def validate(self) -> list[str]:
        """Return a list of error strings (never raises)."""
        errors: list[str] = []
        if self.response_type not in ("question", "brief"):
            errors.append(
                f"response_type must be 'question' or 'brief', got '{self.response_type}'"
            )
        if self.response_type == "brief" and self.ready_to_build and not self.build_brief.strip():
            errors.append(
                "build_brief must not be empty when response_type is 'brief' "
                "and ready_to_build is True"
            )
        return errors

    def to_dict(self) -> dict[str, object]:
        return {
            "response_type": self.response_type,
            "message": self.message,
            "ready_to_build": self.ready_to_build,
            "build_brief": self.build_brief,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> DroneBuildBrief:
        return DroneBuildBrief(
            response_type=str(data.get("response_type", "")),
            message=str(data.get("message", "")),
            ready_to_build=bool(data.get("ready_to_build", False)),
            build_brief=str(data.get("build_brief", "")),
        )

    def is_ready_to_build(self) -> bool:
        return self.ready_to_build and self.response_type == "brief"
