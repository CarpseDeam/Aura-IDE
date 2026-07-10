"""Built-in project catalog adapters."""

from aura.godot_assets.adapters.ruinlab import RuinLabCatalogSource

BUILTIN_ASSET_SOURCES = (RuinLabCatalogSource(),)

__all__ = ["BUILTIN_ASSET_SOURCES", "RuinLabCatalogSource"]
