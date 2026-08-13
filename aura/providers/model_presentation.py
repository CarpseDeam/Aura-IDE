"""Deterministic display ordering for a provider's model list.

One owner for the policy that used to be duplicated between
``ModelsPage._populate_models`` and ``LeftPane.populate_models`` /
``_models_with_default``: given a provider's discovered ``ModelInfo`` set plus
whatever compatibility ids the caller needs represented (a saved selection, a
provider default), return the ordered, deduplicated list a picker should show.

No Qt here — this is pure data shaping shared by every GUI model picker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from aura.providers.base import ModelId, ModelInfo, ProviderId


@dataclass(frozen=True, slots=True)
class ModelPickerItem:
    """One selectable entry: the value a picker stores and the text it shows."""

    model_id: str
    label: str


def _fallback_label(model_id: str) -> str:
    """Human-readable label for an id that isn't in the discovered catalog."""
    return model_id.split("/")[-1].replace("-", " ").title()


def _openrouter_sort_key(info: ModelInfo) -> tuple[int, int, str]:
    """Newest ``created`` first; missing/zero metadata sorts after, by label.

    Never infers age from the model id or name — only OpenRouter's own
    ``created`` field (see ``ModelInfo.created``) feeds this.
    """
    created = info.created or 0
    has_created = created > 0
    return (0 if has_created else 1, -created, info.label.lower())


def build_model_picker_items(
    provider_id: ProviderId,
    models: dict[ModelId, ModelInfo],
    *,
    default_model: str = "",
    current_selection: str = "",
    extra_compat_ids: Sequence[str] = (),
) -> list[ModelPickerItem]:
    """Return the ordered, deduplicated items a model picker should display.

    OpenRouter models sort newest-``created``-first with a deterministic
    label fallback for ties/missing metadata; every other provider keeps the
    order ``models`` was already given in. ``default_model``,
    ``current_selection``, and ``extra_compat_ids`` are appended as
    compatibility entries when not already present in *models* — e.g. a saved
    selection that no longer exists upstream — but are never written back
    into *models* itself, so a removed OpenRouter model doesn't survive
    merely because it was once a default or a prior selection.
    """
    entries = list(models.values())
    if provider_id == "openrouter":
        entries = sorted(entries, key=_openrouter_sort_key)

    seen: set[str] = set()
    items: list[ModelPickerItem] = []

    def add(mid: str, label: str = "") -> None:
        if not mid or mid in seen:
            return
        seen.add(mid)
        items.append(ModelPickerItem(model_id=mid, label=label or _fallback_label(mid)))

    for info in entries:
        add(info.id, info.label)

    if default_model:
        add(default_model)
    if current_selection:
        add(current_selection)
    for mid in extra_compat_ids:
        add(mid)

    return items
