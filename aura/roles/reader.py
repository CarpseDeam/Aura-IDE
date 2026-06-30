"""Reader for bundled role posture capsules."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aura.context_gearbox.models import RuntimeRole

_SAFE_BUNDLED_NAME_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class RoleCapsule:
    role: RuntimeRole
    source: str
    content: str
    checksum: str


@dataclass(frozen=True)
class NamedRoleCapsule:
    name: str
    source: str
    content: str
    checksum: str


def _read_bundled_markdown(name: str) -> tuple[Path, str] | None:
    if not _SAFE_BUNDLED_NAME_RE.fullmatch(name):
        return None
    path = Path(__file__).with_name("bundled") / f"{name}.md"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content:
        return None
    return path, content


def load_bundled_role_capsule(role: RuntimeRole | str) -> RoleCapsule | None:
    """Load a bundled markdown role capsule, if one is available."""
    from aura.context_gearbox.models import RuntimeRole

    try:
        runtime_role = RuntimeRole.from_value(role)
    except ValueError:
        return None

    loaded = _read_bundled_markdown(runtime_role.value)
    if loaded is None:
        return None
    path, content = loaded

    return RoleCapsule(
        role=runtime_role,
        source=str(path),
        content=content,
        checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def load_bundled_named_role_capsule(
    name: str,
    allowed: set[str] | None = None,
) -> NamedRoleCapsule | None:
    """Load a safe named internal role capsule, if one is available."""
    normalized = str(name or "").strip().lower()
    if not normalized or not _SAFE_BUNDLED_NAME_RE.fullmatch(normalized):
        return None
    if allowed is not None and normalized not in allowed:
        return None

    loaded = _read_bundled_markdown(normalized)
    if loaded is None:
        return None
    path, content = loaded
    return NamedRoleCapsule(
        name=normalized,
        source=str(path),
        content=content,
        checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
