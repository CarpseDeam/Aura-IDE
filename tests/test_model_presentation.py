"""Model-picker presentation ordering: newest-first OpenRouter, stable elsewhere."""

from __future__ import annotations

from aura.providers.base import ModelInfo
from aura.providers.model_presentation import ModelPickerItem, build_model_picker_items


def _model(mid: str, label: str | None = None, created: int | None = None) -> ModelInfo:
    return ModelInfo(
        id=mid,
        label=label or mid,
        input_per_m_usd=1.0,
        output_per_m_usd=2.0,
        cache_hit_per_m_usd=0.5,
        created=created,
    )


class TestOpenRouterOrdering:
    def test_newer_created_sorts_first(self):
        models = {
            "old": _model("old", created=1000),
            "new": _model("new", created=2000),
            "mid": _model("mid", created=1500),
        }
        items = build_model_picker_items("openrouter", models)
        assert [i.model_id for i in items] == ["new", "mid", "old"]

    def test_missing_created_sorts_after_dated_entries(self):
        models = {
            "dated": _model("dated", created=1000),
            "undated": _model("undated", created=None),
        }
        items = build_model_picker_items("openrouter", models)
        assert [i.model_id for i in items] == ["dated", "undated"]

    def test_ties_and_missing_metadata_break_deterministically_by_label(self):
        models = {
            "z-model": _model("z-model", label="Zeta", created=None),
            "a-model": _model("a-model", label="Alpha", created=None),
            "same-time-b": _model("same-time-b", label="Bravo", created=5000),
            "same-time-a": _model("same-time-a", label="Alfa", created=5000),
        }
        items = build_model_picker_items("openrouter", models)
        ids = [i.model_id for i in items]
        # Dated entries (same timestamp) come first, tie-broken by label.
        assert ids[:2] == ["same-time-a", "same-time-b"]
        # Undated entries come last, tie-broken by label.
        assert ids[2:] == ["a-model", "z-model"]

    def test_does_not_infer_order_from_model_id_text(self):
        # "zzz-2099" looks newest by name but carries no real timestamp, while
        # "aaa-2001" has real (older) upstream metadata — real metadata wins.
        models = {
            "zzz-2099": _model("zzz-2099", created=None),
            "aaa-2001": _model("aaa-2001", created=100),
        }
        items = build_model_picker_items("openrouter", models)
        assert [i.model_id for i in items] == ["aaa-2001", "zzz-2099"]


class TestOtherProviderOrderingUnaffected:
    def test_non_openrouter_preserves_insertion_order(self):
        models = {
            "b": _model("b", created=9999),  # created is ignored for non-OpenRouter
            "a": _model("a", created=1),
            "c": _model("c"),
        }
        items = build_model_picker_items("deepseek", models)
        assert [i.model_id for i in items] == ["b", "a", "c"]


class TestCompatibilityEntries:
    def test_default_and_current_selection_appended_when_missing(self):
        models = {"a": _model("a")}
        items = build_model_picker_items(
            "openrouter",
            models,
            default_model="default-mid",
            current_selection="saved-mid",
        )
        ids = [i.model_id for i in items]
        assert ids == ["a", "default-mid", "saved-mid"]

    def test_compat_entries_not_duplicated_when_already_present(self):
        models = {"a": _model("a")}
        items = build_model_picker_items(
            "openrouter", models, default_model="a", current_selection="a"
        )
        assert [i.model_id for i in items] == ["a"]

    def test_extra_compat_ids_appended(self):
        models = {"a": _model("a")}
        items = build_model_picker_items(
            "deepseek", models, extra_compat_ids=("compat-mid",)
        )
        assert [i.model_id for i in items] == ["a", "compat-mid"]

    def test_compat_entries_do_not_mutate_source_models_dict(self):
        models = {"a": _model("a")}
        build_model_picker_items(
            "openrouter", models, default_model="not-in-catalog"
        )
        assert "not-in-catalog" not in models

    def test_returns_model_picker_items(self):
        items = build_model_picker_items("deepseek", {"a": _model("a", label="Alpha")})
        assert items == [ModelPickerItem(model_id="a", label="Alpha")]
