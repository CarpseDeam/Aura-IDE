"""Provider registry — single source of truth for all AI providers.

Consumes ``PROVIDER_CATALOG`` and wraps each entry in a ``ProviderSpec``.
The ``models`` and ``pricing`` dicts inside each spec are shared references
to the module-level dicts in ``catalog.py``, so dynamic catalog loading
propagates automatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aura.providers.base import ProviderSpec
from aura.providers.catalog import PROVIDER_CATALOG

if TYPE_CHECKING:
    from aura.client.deepseek import DeepSeekClient
    from aura.providers.google_cloud.client import GoogleCloudClient


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
                kind=raw.get("kind", "api_key"),
                chat_protocol=raw.get("chat_protocol", "openai_chat"),
                chat_base_url=raw.get("chat_base_url"),
                requires_reasoning_replay=raw.get("requires_reasoning_replay", True),
            )

    def ids(self) -> list[str]:
        return list(self._providers.keys())

    def has(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def get(self, provider_id: str) -> ProviderSpec:
        return self._providers[provider_id]

    def all(self) -> dict[str, ProviderSpec]:
        return dict(self._providers)

    def create_client(self, provider_id: str) -> DeepSeekClient | GoogleCloudClient:
        """Build the API client for *provider_id*.

        Only ``api_key`` providers have an API client.  An unregistered id, or
        a provider of any other kind, raises instead of falling through to the
        DeepSeek client — a wrong-provider client would otherwise send the
        turn to DeepSeek under another provider's name.
        """
        if provider_id not in self._providers:
            raise ValueError(f"Unknown provider {provider_id!r}: no client can be created.")

        spec = self._providers[provider_id]
        if spec.kind != "api_key":
            raise ValueError(
                f"{spec.label} ({provider_id}) is a {spec.kind!r} provider and has no "
                f"API client. Select an API-key provider in Settings -> Provider Setup."
            )

        if provider_id == "google_cloud":
            from aura.providers.google_cloud.client import GoogleCloudClient
            from aura.providers.google_cloud.config import (
                get_google_cloud_location,
                get_google_cloud_project,
            )
            from aura.config import get_api_key

            return GoogleCloudClient(
                project=get_google_cloud_project(),
                location=get_google_cloud_location(),
                api_key=get_api_key("google_cloud"),
            )
        from aura.client.deepseek import DeepSeekClient

        return DeepSeekClient(provider=provider_id)


provider_registry = ProviderRegistry()
