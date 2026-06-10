from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CapabilityRequirement:
    """A capability that a drone needs to fulfill."""

    capability: str
    purpose: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "purpose": self.purpose,
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> CapabilityRequirement:
        return CapabilityRequirement(
            capability=str(data.get("capability", "")),
            purpose=str(data.get("purpose", "")),
            notes=str(data.get("notes", "")),
        )


@dataclass(frozen=True)
class CapabilityCandidate:
    """A candidate route for fulfilling a capability."""

    capability: str
    route_kind: str
    source: str
    confidence: float = 0.0
    setup_required: bool = False
    setup_notes: str = ""
    tool_names: tuple[str, ...] = ()
    install_command: str = ""
    docs_url: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "route_kind": self.route_kind,
            "source": self.source,
            "confidence": self.confidence,
            "setup_required": self.setup_required,
            "setup_notes": self.setup_notes,
            "tool_names": list(self.tool_names),
            "install_command": self.install_command,
            "docs_url": self.docs_url,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> CapabilityCandidate:
        tool_names_raw = data.get("tool_names", ())
        if isinstance(tool_names_raw, list):
            tool_names = tuple(str(x) for x in tool_names_raw)
        else:
            tool_names = tuple(str(x) for x in tool_names_raw)  # type: ignore[arg-type]

        return CapabilityCandidate(
            capability=str(data.get("capability", "")),
            route_kind=str(data.get("route_kind", "")),
            source=str(data.get("source", "")),
            confidence=float(data.get("confidence", 0.0)),
            setup_required=bool(data.get("setup_required", False)),
            setup_notes=str(data.get("setup_notes", "")),
            tool_names=tool_names,
            install_command=str(data.get("install_command", "")),
            docs_url=str(data.get("docs_url", "")),
        )


@dataclass(frozen=True)
class CapabilityBinding:
    """A resolved binding between a requirement and a chosen candidate."""

    capability: str
    route_kind: str
    source: str
    tool_names: tuple[str, ...] = ()
    setup_status: str = "pending"
    setup_notes: str = ""
    command: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "route_kind": self.route_kind,
            "source": self.source,
            "tool_names": list(self.tool_names),
            "setup_status": self.setup_status,
            "setup_notes": self.setup_notes,
            "command": self.command,
            "metadata": dict(self.metadata),
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> CapabilityBinding:
        tool_names_raw = data.get("tool_names", ())
        if isinstance(tool_names_raw, list):
            tool_names = tuple(str(x) for x in tool_names_raw)
        else:
            tool_names = tuple(str(x) for x in tool_names_raw)  # type: ignore[arg-type]

        metadata_raw = data.get("metadata", {})
        metadata: dict[str, object] = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}

        return CapabilityBinding(
            capability=str(data.get("capability", "")),
            route_kind=str(data.get("route_kind", "")),
            source=str(data.get("source", "")),
            tool_names=tool_names,
            setup_status=str(data.get("setup_status", "pending")),
            setup_notes=str(data.get("setup_notes", "")),
            command=str(data.get("command", "")),
            metadata=metadata,
        )


@dataclass(frozen=True)
class CapabilityResolution:
    """Complete resolution of drone capability requirements."""

    requirements: tuple[CapabilityRequirement, ...]
    candidates: tuple[CapabilityCandidate, ...]
    selected_bindings: tuple[CapabilityBinding, ...]
    allowed_tools: tuple[str, ...]
    setup_notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "requirements": [r.to_dict() for r in self.requirements],
            "candidates": [c.to_dict() for c in self.candidates],
            "selected_bindings": [b.to_dict() for b in self.selected_bindings],
            "allowed_tools": list(self.allowed_tools),
            "setup_notes": list(self.setup_notes),
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> CapabilityResolution:
        requirements_raw = data.get("requirements", ())
        requirements = tuple(
            CapabilityRequirement.from_dict(r) if isinstance(r, dict) else r
            for r in requirements_raw
        )

        candidates_raw = data.get("candidates", ())
        candidates = tuple(
            CapabilityCandidate.from_dict(c) if isinstance(c, dict) else c
            for c in candidates_raw
        )

        bindings_raw = data.get("selected_bindings", ())
        selected_bindings = tuple(
            CapabilityBinding.from_dict(b) if isinstance(b, dict) else b
            for b in bindings_raw
        )

        allowed_tools_raw = data.get("allowed_tools", ())
        if isinstance(allowed_tools_raw, list):
            allowed_tools = tuple(str(x) for x in allowed_tools_raw)
        else:
            allowed_tools = tuple(str(x) for x in allowed_tools_raw)  # type: ignore[arg-type]

        setup_notes_raw = data.get("setup_notes", ())
        if isinstance(setup_notes_raw, list):
            setup_notes = tuple(str(x) for x in setup_notes_raw)
        else:
            setup_notes = tuple(str(x) for x in setup_notes_raw)  # type: ignore[arg-type]

        return CapabilityResolution(
            requirements=requirements,
            candidates=candidates,
            selected_bindings=selected_bindings,
            allowed_tools=allowed_tools,
            setup_notes=setup_notes,
        )
