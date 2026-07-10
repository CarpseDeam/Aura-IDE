"""Discover, query, and summarize read-only Godot asset catalog adapters."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from aura.godot_assets.adapters import BUILTIN_ASSET_SOURCES
from aura.godot_assets.models import AssetCatalogSnapshot, GodotAsset, GodotAssetCatalogSource


def inspect_godot_assets(
    project_root: Path,
    *,
    domains: Iterable[str] = (),
    kinds: Iterable[str] = (),
    tags: Iterable[str] = (),
    semantic_roles: Iterable[str] = (),
    query: str = "",
    max_items: int = 50,
    sources: Iterable[GodotAssetCatalogSource] = BUILTIN_ASSET_SOURCES,
) -> dict[str, Any]:
    """Return a bounded deterministic view of every recognized project asset source."""
    root = project_root.resolve()
    if not (root / "project.godot").is_file():
        return {"ok": False, "error": "workspace is not a Godot project (project.godot is missing)"}
    max_items = max(1, min(int(max_items), 200))
    snapshots = [source.load(root) for source in sources if source.is_available(root)]
    if not snapshots:
        return {
            "ok": False,
            "error": "no supported Godot asset catalog was discovered",
            "supported_sources": [source.name for source in sources],
        }
    merged = _merge_snapshots(snapshots)
    all_assets = list(merged.assets)
    matched = [
        asset
        for asset in all_assets
        if _matches(asset, domains, kinds, tags, semantic_roles, query)
    ]
    returned = matched[:max_items]
    return {
        "ok": True,
        "read_only": True,
        "sources": list(merged.sources),
        "catalog_asset_count": len(all_assets),
        "matched_asset_count": len(matched),
        "returned_asset_count": len(returned),
        "truncated": len(returned) < len(matched),
        "available_domains": sorted({asset.domain for asset in all_assets}),
        "kind_counts": dict(sorted(Counter(asset.kind for asset in all_assets).items())),
        "semantic_role_counts": dict(
            sorted(Counter(role for asset in all_assets for role in asset.semantic_roles).items())
        ),
        "diagnostic_count": len(merged.diagnostics),
        "diagnostics": list(merged.diagnostics[:100]),
        "assets": [asset.to_dict() for asset in returned],
    }


def _merge_snapshots(snapshots: list[AssetCatalogSnapshot]) -> AssetCatalogSnapshot:
    sources: list[str] = []
    assets: list[GodotAsset] = []
    diagnostics: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for snapshot in snapshots:
        sources.extend(snapshot.sources)
        diagnostics.extend(snapshot.diagnostics)
        for asset in snapshot.assets:
            identity = (asset.domain.casefold(), asset.id.casefold())
            if identity in identities:
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "duplicate_asset_identity",
                        "asset_id": asset.id,
                        "message": f"duplicate asset identity across sources: {asset.domain}:{asset.id}",
                    }
                )
                continue
            identities.add(identity)
            assets.append(asset)
    assets.sort(key=lambda asset: (asset.domain.casefold(), asset.id.casefold()))
    return AssetCatalogSnapshot(tuple(dict.fromkeys(sources)), tuple(assets), tuple(diagnostics))


def _matches(
    asset: GodotAsset,
    domains: Iterable[str],
    kinds: Iterable[str],
    tags: Iterable[str],
    semantic_roles: Iterable[str],
    query: str,
) -> bool:
    wanted_domains = _normalized(domains)
    wanted_kinds = _normalized(kinds)
    wanted_tags = _normalized(tags)
    wanted_roles = _normalized(semantic_roles)
    if wanted_domains and asset.domain.casefold() not in wanted_domains:
        return False
    if wanted_kinds and asset.kind.casefold() not in wanted_kinds:
        return False
    if wanted_tags and not wanted_tags.issubset(tag.casefold() for tag in asset.tags):
        return False
    if wanted_roles and not wanted_roles.issubset(role.casefold() for role in asset.semantic_roles):
        return False
    needle = query.strip().casefold()
    if needle:
        haystack = " ".join(
            (asset.id, asset.resource_path, asset.domain, asset.kind, *asset.tags, *asset.semantic_roles)
        ).casefold()
        if needle not in haystack:
            return False
    return True


def _normalized(values: Iterable[str]) -> set[str]:
    return {str(value).strip().casefold() for value in values if str(value).strip()}


__all__ = ["inspect_godot_assets"]
