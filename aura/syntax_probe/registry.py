from __future__ import annotations

from pathlib import Path
from typing import Iterator

from aura.syntax_probe.protocol import SyntaxProbe

# Module-level registry of SyntaxProbe subclasses.
# Iteration is in registration order; *get_probe* uses reverse iteration so
# the last-registered probe for a given file suffix wins.
REGISTRY: list[type[SyntaxProbe]] = []


def register_probe(probe_cls: type[SyntaxProbe]) -> None:
    """Register a SyntaxProbe subclass.

    Probes are checked in reverse registration order; the **last**-registered
    probe that matches a file wins.
    """
    REGISTRY.append(probe_cls)


def iter_probes() -> Iterator[type[SyntaxProbe]]:
    """Yield registered probe classes in registration order."""
    return iter(REGISTRY)


def get_probe(file_path: str | Path) -> type[SyntaxProbe] | None:
    """Return the last-registered probe that claims *file_path*, or None."""
    for probe_cls in reversed(REGISTRY):
        if probe_cls.detect(file_path):
            return probe_cls
    return None
