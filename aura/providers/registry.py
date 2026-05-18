"""Provider registry — single source of truth for all AI providers.

Consumes ``PROVIDER_CATALOG`` and wraps each entry in a ``ProviderSpec``.
The ``models`` and ``pricing`` dicts inside each spec are shared references
to the module-level dicts in ``catalog.py``, so dynamic catalog loading
propagates automatically.
"""

from __future__ import annotations

from aura.providers.base import ProviderSpec
from aura.providers.catalog import PROVIDER_CATALOG


class ProviderRegistry:
    def __init__(self, catalog: dict[str, dict] | None = None) -> None:
        self._providers: dict[str, ProviderSpec] = {}
        source = catalog if catalog is not None else PROVIDER_CATALOG
        for pid, raw in source.items():
            self._providers[pid] = ProviderSpec(
                id=pid,
                label=raw["label"],
                base_url=raw["base_url"],
                env_key=raw["env_key"],
                default_model=raw["default_model"],
                default_thinking=raw["default_thinking"],
                models=raw["models"],
                pricing=raw["pricing"],
            )

    def ids(self) -> list[str]:
        return list(self._providers.keys())

    def has(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def get(self, provider_id: str) -> ProviderSpec:
        return self._providers[provider_id]

    def all(self) -> dict[str, ProviderSpec]:
        return dict(self._providers)

    def create_client(self, provider_id: str) -> "DeepSeekClient":
        from aura.client.deepseek import DeepSeekClient

        return DeepSeekClient(provider=provider_id)


provider_registry = ProviderRegistry()
