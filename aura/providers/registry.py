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
from aura.providers.local_openai import normalize_local_openai_base_url

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

    def configure_local_base_url(self, base_url: object) -> str:
        """Apply the persisted Local Model endpoint to the runtime spec.

        Invalid values remain visible in the spec so usability checks fail and
        Settings can show the value that needs correction. Client creation is
        the validation boundary.
        """
        normalized = normalize_local_openai_base_url(base_url)
        spec = self._providers.get("local_openai")
        if spec is not None:
            spec.base_url = normalized
        return normalized

    def create_client(
        self,
        provider_id: str,
        *,
        base_url: str | None = None,
    ) -> DeepSeekClient | GoogleCloudClient:
        """Build the API client for *provider_id*.

        API-key providers and the OpenAI-compatible local provider share the
        production client transport. An unregistered id or unsupported kind
        raises instead of falling through to DeepSeek. ``base_url`` is an
        unsaved discovery override and is accepted only for a local provider.
        """
        if provider_id not in self._providers:
            raise ValueError(f"Unknown provider {provider_id!r}: no client can be created.")

        spec = self._providers[provider_id]
        if spec.kind not in {"api_key", "local"}:
            raise ValueError(
                f"{spec.label} ({provider_id}) is a {spec.kind!r} provider and has no "
                "API client. Select a supported provider in Settings."
            )

        if base_url is not None and spec.kind != "local":
            raise ValueError("A base URL override is only supported for local providers.")

        if provider_id == "google_cloud":
            from aura.config import get_api_key
            from aura.providers.google_cloud.client import GoogleCloudClient
            from aura.providers.google_cloud.config import (
                get_google_cloud_location,
                get_google_cloud_project,
            )

            return GoogleCloudClient(
                project=get_google_cloud_project(),
                location=get_google_cloud_location(),
                api_key=get_api_key("google_cloud"),
            )
        from aura.client.deepseek import DeepSeekClient

        return DeepSeekClient(provider=provider_id, base_url=base_url)


provider_registry = ProviderRegistry()
