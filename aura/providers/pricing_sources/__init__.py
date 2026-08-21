"""Pricing sources — provider-owned retrievers of official published rates.

Importing this package registers every available source with the pricing
boundary (``aura.providers.pricing``). Each source class owns its provider's
rates; the boundary owns validation, the last-known-good cache, and the
normalized result the cost path reads.
"""

from __future__ import annotations

from aura.providers.pricing import register_source
from aura.providers.pricing_sources.deepseek import DeepSeekPricingSource

register_source(DeepSeekPricingSource)

__all__ = ["DeepSeekPricingSource"]
