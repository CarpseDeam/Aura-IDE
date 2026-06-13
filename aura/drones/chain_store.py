from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import asdict, fields
from pathlib import Path

from aura.drones.chain import ChainDefinition, ChainEdge, ChainNode
from aura.drones.definition import slugify
from aura.paths import data_dir

logger = logging.getLogger(__name__)

_CHAIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _is_safe_chain_id(chain_id: str) -> bool:
    return bool(_CHAIN_ID_RE.fullmatch(str(chain_id or "")))


def _chain_from_dict(data: dict) -> ChainDefinition:
    """Reconstruct a ChainDefinition from a JSON-deserialized dict.

    Converts lists back to tuples and handles position list→tuple.
    Filters to known fields for forward compatibility.
    """
    nodes_list: list[ChainNode] = []
    for n in data.get("nodes", []):
        pos = n.get("position", [0.0, 0.0])
        if isinstance(pos, list):
            pos = tuple(pos)
        nodes_list.append(
            ChainNode(
                id=n["id"],
                drone_id=n.get("drone_id", "__draft__"),
                goal_template=n.get("goal_template", ""),
                position=pos,
                is_draft=n.get("is_draft", False),
                draft_name=n.get("draft_name", ""),
                draft_accepts=n.get("draft_accepts", ""),
                draft_produces=n.get("draft_produces", ""),
                draft_brief=n.get("draft_brief", ""),
                is_assignment=n.get("is_assignment", False),
            )
        )

    edges_list = [
        ChainEdge(from_node=e["from_node"], to_node=e["to_node"])
        for e in data.get("edges", [])
    ]

    known_fields = {f.name for f in fields(ChainDefinition)}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    filtered["nodes"] = tuple(nodes_list)
    filtered["edges"] = tuple(edges_list)
    return ChainDefinition(**filtered)


class ChainStore:
    """Read/write Chain definitions from/to data_dir()/chains/.

    All methods are static; workspace_root is always passed explicitly
    for consistency with DroneStore.
    """

    @staticmethod
    def chains_dir() -> Path:
        """Return the chains storage directory without creating it."""
        return data_dir() / "chains"

    @staticmethod
    def _ensure_chains_dir() -> Path:
        """Create and return the chains storage directory."""
        d = data_dir() / "chains"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def save_chain(workspace_root: Path, chain: ChainDefinition) -> None:
        """Persist a chain definition to disk.

        Validates the chain fields before writing. Uses atomic write
        via tempfile.mkstemp + rename.
        """
        ChainStore.validate_chain(chain)
        chains_dir = ChainStore._ensure_chains_dir()
        chain_dir = chains_dir / chain.id
        chain_dir.mkdir(parents=True, exist_ok=True)
        p = chain_dir / "chain.json"
        data = asdict(chain)
        fd, tmp_path = tempfile.mkstemp(dir=str(chain_dir), suffix=".json")
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        Path(tmp_path).replace(p)

    @staticmethod
    def load_chain(workspace_root: Path, chain_id: str) -> ChainDefinition | None:
        """Load a chain definition by id. Returns None if not found or invalid."""
        if not _is_safe_chain_id(chain_id):
            return None
        chain_file = ChainStore.chains_dir() / chain_id / "chain.json"
        if not chain_file.exists():
            return None
        try:
            data = json.loads(chain_file.read_text(encoding="utf-8"))
            return _chain_from_dict(data)
        except Exception:
            logger.warning("Failed to load chain %s", chain_id)
            return None

    @staticmethod
    def list_chains(workspace_root: Path) -> list[ChainDefinition]:
        """List all saved chain definitions, sorted by name."""
        chains_dir = ChainStore.chains_dir()
        if not chains_dir.exists():
            return []
        result: list[ChainDefinition] = []
        for subdir in sorted(chains_dir.iterdir()):
            if not subdir.is_dir():
                continue
            chain_file = subdir / "chain.json"
            if not chain_file.exists():
                continue
            try:
                data = json.loads(chain_file.read_text(encoding="utf-8"))
                chain = _chain_from_dict(data)
                result.append(chain)
            except Exception:
                logger.warning("Skipping invalid chain: %s", subdir.name)
        result.sort(key=lambda c: c.name)
        return result

    @staticmethod
    def delete_chain(workspace_root: Path, chain_id: str) -> bool:
        """Remove a chain definition. Returns True if anything was deleted."""
        if not _is_safe_chain_id(chain_id):
            return False
        chain_dir = ChainStore.chains_dir() / chain_id
        chain_file = chain_dir / "chain.json"
        if not chain_file.exists():
            return False
        chain_file.unlink()
        try:
            chain_dir.rmdir()
        except OSError:
            pass
        return True

    @staticmethod
    def next_id(workspace_root: Path, name: str) -> str:
        """Generate the next available chain id from a name."""
        base = slugify(name)
        if not base:
            base = "chain"

        candidate = base
        counter = 0
        chains_dir = ChainStore.chains_dir()

        while True:
            chain_file = chains_dir / candidate / "chain.json"
            if not chain_file.exists():
                return candidate
            counter += 1
            candidate = f"{base}-{counter}"

    @staticmethod
    def validate_chain(chain: ChainDefinition) -> None:
        """Validate basic chain fields before saving."""
        if not _is_safe_chain_id(chain.id):
            raise ValueError(
                "Chain id must be lowercase letters, numbers, and hyphens"
            )
        if not chain.name.strip():
            raise ValueError("Chain name is required")
        if not chain.description.strip():
            raise ValueError("Chain description is required")


# ---------------------------------------------------------------------------
# Module-level convenience functions used by GUI components.
# These operate on raw dicts so that extra fields (e.g. auto_route) survive
# the round-trip without being filtered by ChainDefinition's field list.
# ---------------------------------------------------------------------------

def load_chain(workspace_root: Path, chain_id: str) -> dict | None:
    """Load a chain as a raw dict. Returns None if not found."""
    if not _is_safe_chain_id(chain_id):
        return None
    chain_file = ChainStore.chains_dir() / chain_id / "chain.json"
    if not chain_file.exists():
        return None
    try:
        return json.loads(chain_file.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to load chain %s", chain_id)
        return None


def save_chain(workspace_root: Path, chain_id: str | None, data: dict) -> str:
    """Persist a chain dict to disk. Generates an id if chain_id is None.

    Returns the chain id used (new or existing).
    """
    if chain_id is None:
        chain_id = ChainStore.next_id(workspace_root, data.get("name", "chain"))
    if not _is_safe_chain_id(chain_id):
        raise ValueError(f"Invalid chain id: {chain_id!r}")
    data = dict(data)
    data["id"] = chain_id
    if not data.get("name", "").strip():
        data["name"] = chain_id
    chains_dir = ChainStore._ensure_chains_dir()
    chain_dir = chains_dir / chain_id
    chain_dir.mkdir(parents=True, exist_ok=True)
    p = chain_dir / "chain.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(chain_dir), suffix=".json")
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    Path(tmp_path).replace(p)
    return chain_id


def delete_chain(workspace_root: Path, chain_id: str) -> bool:
    """Remove a chain. Returns True if anything was deleted."""
    return ChainStore.delete_chain(workspace_root, chain_id)


def list_chains(workspace_root: Path) -> list[dict]:
    """Return all saved chains as raw dicts, sorted by name."""
    chains_dir = ChainStore.chains_dir()
    if not chains_dir.exists():
        return []
    result: list[dict] = []
    for subdir in sorted(chains_dir.iterdir()):
        if not subdir.is_dir():
            continue
        chain_file = subdir / "chain.json"
        if not chain_file.exists():
            continue
        try:
            data = json.loads(chain_file.read_text(encoding="utf-8"))
            result.append(data)
        except Exception:
            logger.warning("Skipping invalid chain: %s", subdir.name)
    result.sort(key=lambda c: c.get("name", ""))
    return result
