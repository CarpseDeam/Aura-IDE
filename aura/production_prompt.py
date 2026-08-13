"""Load Aura's bundled production prompt."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProductionPrompt:
    source: str
    content: str
    checksum: str


def _try_read_markdown(path: Path) -> tuple[Path, str] | None:
    """Read and strip a markdown file, returning None on failure or empty."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content:
        return None
    return path, content


def _read_production_prompt_markdown() -> tuple[Path, str] | None:
    # 1. Dev path next to the loader.
    local_path = Path(__file__).with_name("production_prompt.md")
    result = _try_read_markdown(local_path)
    if result is not None:
        return result

    # 2. Packaged-resource fallback (wheel / Nuitka)
    from aura.resources import get_resource_path

    resource_path = get_resource_path(Path("aura") / "production_prompt.md")
    return _try_read_markdown(resource_path)


def load_production_prompt() -> ProductionPrompt | None:
    """Load Aura's one bundled production prompt."""
    loaded = _read_production_prompt_markdown()
    if loaded is None:
        return None
    path, content = loaded

    return ProductionPrompt(
        source=str(path),
        content=content,
        checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
