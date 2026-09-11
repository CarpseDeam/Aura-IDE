"""OpenRouter snapshot-replacement semantics: discovery refresh + cache.

Covers the actual bug this task fixes: ``cfg.models.update(...)`` never
deletes stale keys, so a model OpenRouter removed upstream survived forever.
Both the live-discovery path (``ModelsPage._on_discovery_finished``) and the
dynamic-cache load path (``load_dynamic_catalog``) must instead treat a
successful OpenRouter response as a full snapshot.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from aura.config import (
    AppSettings,
    catalog_cache_path,
    load_dynamic_catalog,
    save_dynamic_catalog,
)
from aura.providers.base import ModelInfo
from aura.providers.registry import provider_registry


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def openrouter_snapshot():
    """Snapshot + restore the shared OpenRouter models/pricing dicts.

    ``cfg.models``/``cfg.pricing`` are the actual shared dict objects backing
    the global provider registry (see ``aura.providers.catalog``), so tests
    that mutate them must not leak state into other tests.
    """
    cfg = provider_registry.get("openrouter")
    models_before = dict(cfg.models)
    pricing_before = dict(cfg.pricing)
    yield cfg
    cfg.models.clear()
    cfg.models.update(models_before)
    cfg.pricing.clear()
    cfg.pricing.update(pricing_before)


def _model(mid: str, created: int | None = None) -> ModelInfo:
    return ModelInfo(
        id=mid,
        label=mid,
        input_per_m_usd=1.0,
        output_per_m_usd=2.0,
        cache_hit_per_m_usd=0.5,
        created=created,
    )


def _pricing(in_miss: float = 1.0, out: float = 2.0) -> dict[str, float]:
    return {"in_miss": in_miss, "in_hit": 0.5, "out": out}


class TestModelsPageDiscoveryReplace:
    def _make_page(self, monkeypatch, qapp):
        from aura.gui.settings_pages.models_page import ModelsPage

        monkeypatch.setattr(ModelsPage, "_start_discovery", lambda self, _pid: None)
        page = ModelsPage(AppSettings(provider="openrouter"))
        return page

    def test_successful_discovery_replaces_stale_models(
        self, qapp, monkeypatch, openrouter_snapshot
    ):
        cfg = openrouter_snapshot
        cfg.models.clear()
        cfg.models.update({"a": _model("a"), "b": _model("b")})
        cfg.pricing.clear()
        cfg.pricing.update({"a": _pricing(), "b": _pricing()})

        page = self._make_page(monkeypatch, qapp)
        try:
            page._on_discovery_finished(
                "openrouter",
                {"b": _model("b"), "c": _model("c")},
                {"b": _pricing(), "c": _pricing()},
                "",
            )

            assert set(cfg.models.keys()) == {"b", "c"}
            assert "a" not in cfg.models
            assert set(cfg.pricing.keys()) == {"b", "c"}
        finally:
            page.cleanup_threads()
            page.deleteLater()

    def test_failed_discovery_preserves_existing_state(
        self, qapp, monkeypatch, openrouter_snapshot
    ):
        cfg = openrouter_snapshot
        cfg.models.clear()
        cfg.models.update({"a": _model("a")})
        cfg.pricing.clear()
        cfg.pricing.update({"a": _pricing()})

        page = self._make_page(monkeypatch, qapp)
        try:
            page._on_discovery_finished("openrouter", {}, {}, "network error")
            assert set(cfg.models.keys()) == {"a"}

            page._on_discovery_finished("openrouter", {}, {}, "")
            assert set(cfg.models.keys()) == {"a"}
        finally:
            page.cleanup_threads()
            page.deleteLater()


class TestDynamicCacheSnapshot:
    def test_save_then_load_excludes_stale_seed_entries(
        self, tmp_path, monkeypatch, openrouter_snapshot
    ):
        monkeypatch.setenv("AURA_CONFIG_DIR", str(tmp_path))
        cfg = openrouter_snapshot
        cfg.models.clear()
        cfg.models.update({"a": _model("a"), "b": _model("b")})
        cfg.pricing.clear()
        cfg.pricing.update({"a": _pricing(), "b": _pricing()})

        # Simulate a successful discovery snapshot of B and C only.
        save_dynamic_catalog(
            "openrouter",
            {"b": _model("b"), "c": _model("c")},
            {"b": _pricing(), "c": _pricing()},
        )

        # Seeded catalog still has stale "a" until load runs.
        assert "a" in cfg.models

        load_dynamic_catalog()

        assert set(cfg.models.keys()) == {"b", "c"}
        assert "a" not in cfg.models
        assert set(cfg.pricing.keys()) == {"b", "c"}

    def test_zero_pricing_preserved_as_free(
        self, tmp_path, monkeypatch, openrouter_snapshot
    ):
        monkeypatch.setenv("AURA_CONFIG_DIR", str(tmp_path))
        cfg = openrouter_snapshot
        cfg.models.clear()
        cfg.pricing.clear()

        free_model = _model("free/model")
        save_dynamic_catalog(
            "openrouter",
            {"free/model": free_model},
            {"free/model": {"in_miss": 0.0, "in_hit": 0.0, "out": 0.0}},
        )
        load_dynamic_catalog()

        assert cfg.pricing["free/model"] == {"in_miss": 0.0, "in_hit": 0.0, "out": 0.0}

    def test_old_cache_without_created_field_still_loads(
        self, tmp_path, monkeypatch, openrouter_snapshot
    ):
        monkeypatch.setenv("AURA_CONFIG_DIR", str(tmp_path))
        cfg = openrouter_snapshot
        cfg.models.clear()
        cfg.pricing.clear()

        cache_path = catalog_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_model = {
            "id": "legacy/model",
            "label": "Legacy Model",
            "input_per_m_usd": 1.0,
            "output_per_m_usd": 2.0,
            "cache_hit_per_m_usd": 0.5,
            "supports_vision": False,
            "context_window_tokens": 8192,
            "max_output_tokens": 1024,
            # No "created" key — pre-existing cache format.
        }
        cache_path.write_text(
            json.dumps(
                {
                    "openrouter": {
                        "models": {"legacy/model": legacy_model},
                        "pricing": {"legacy/model": _pricing()},
                    }
                }
            ),
            encoding="utf-8",
        )

        load_dynamic_catalog()

        assert "legacy/model" in cfg.models
        assert cfg.models["legacy/model"].created is None

    def test_empty_cache_entry_does_not_wipe_seeded_models(
        self, tmp_path, monkeypatch, openrouter_snapshot
    ):
        monkeypatch.setenv("AURA_CONFIG_DIR", str(tmp_path))
        cfg = openrouter_snapshot
        cfg.models.clear()
        cfg.models.update({"seed": _model("seed")})
        cfg.pricing.clear()
        cfg.pricing.update({"seed": _pricing()})

        cache_path = catalog_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"openrouter": {"models": {}, "pricing": {}}}),
            encoding="utf-8",
        )

        load_dynamic_catalog()

        assert "seed" in cfg.models


def test_discovery_and_cache_preserve_capability_distinctions(tmp_path, monkeypatch, openrouter_snapshot):
    import aura.config as config
    from aura.providers.model_presentation import build_model_picker_items

    monkeypatch.setenv("AURA_CONFIG_DIR", str(tmp_path))
    reasoning = {"supported_efforts": ["xhigh", "high", "low"], "mandatory": True,
                 "default_effort": "high", "default_enabled": True}
    raw = [
        {"id": "tools", "supported_parameters": ["tools", "reasoning"], "reasoning": reasoning},
        {"id": "no-tools", "supported_parameters": []},
        {"id": "unknown"},
    ]
    monkeypatch.setattr(config, "refresh_provider_pricing", lambda _: None)
    monkeypatch.setattr(provider_registry, "create_client", lambda *a, **k: SimpleNamespace(fetch_raw_models=lambda: raw))
    models, pricing, error = config.fetch_provider_models("openrouter")
    assert error is None
    assert [models[mid].supports_tools for mid in ("tools", "no-tools", "unknown")] == [True, False, None]
    save_dynamic_catalog("openrouter", models, pricing)
    load_dynamic_catalog()
    assert openrouter_snapshot.models == models
    assert models["tools"].reasoning == reasoning
    items = {i.model_id: i.label for i in build_model_picker_items("openrouter", models, current_selection="old")}
    assert "Tools supported" in items["tools"]
    assert "No tool support" in items["no-tools"]
    assert "Tool support unknown" in items["unknown"]
    assert "Tool support unknown" in items["old"]


@pytest.mark.parametrize(("metadata", "selection", "expected", "effective"), [
    ({"supported_efforts": ["high", "low"]}, "off", {"enabled": False}, "off"),
    ({"supported_efforts": ["high", "low"]}, "max", {"enabled": True, "effort": "high"}, "high"),
    ({"supported_efforts": ["xhigh", "high"]}, "max", {"enabled": True, "effort": "xhigh"}, "max"),
    ({"supported_efforts": ["low"], "mandatory": True}, "off", {"enabled": True, "effort": "low"}, "high"),
    ({"supported_efforts": None}, "max", {"enabled": True, "effort": "max"}, "max"),
    ({"default_effort": "medium"}, "max", {"enabled": True}, "high"),
])
def test_reasoning_settings_follow_model_metadata(metadata, selection, expected, effective):
    from aura.client.reasoning import openrouter_thinking_options, resolve_reasoning_request

    model = replace(_model("m"), supported_parameters=("tools", "reasoning"), reasoning=metadata)
    request = resolve_reasoning_request("openrouter", selection, model_info=model)
    assert request.extra_body == {"reasoning": expected}
    assert request.reasoning_effort is None
    assert request.thinking == effective
    if metadata.get("mandatory"):
        assert all(o.mode != "off" for o in openrouter_thinking_options(model))


def test_unknown_and_unsupported_reasoning_are_distinct():
    from aura.client.reasoning import resolve_reasoning_request

    assert resolve_reasoning_request("openrouter", "off").extra_body == {"reasoning": {"enabled": False}}
    assert resolve_reasoning_request("openrouter", "high").extra_body == {"reasoning": {"enabled": True}}
    unsupported = replace(_model("m"), supported_parameters=("tools",))
    request = resolve_reasoning_request("openrouter", "high", model_info=unsupported)
    assert request.extra_body is None
    assert request.thinking == "off"


def test_model_changes_refresh_thinking_controls(qapp, monkeypatch, openrouter_snapshot):
    from aura.gui.agents_editor import AgentEditor, ModelChoices, ModelTargetChoice
    from aura.gui.left_pane import LeftPane
    from aura.gui.settings_pages.models_page import ModelsPage

    monkeypatch.setattr(ModelsPage, "_start_discovery", lambda *a, **k: None)
    cfg = openrouter_snapshot
    cfg.models.clear()
    cfg.models.update({
        "required": replace(_model("required"), reasoning={"mandatory": True, "supported_efforts": ["high"]}),
        "optional": replace(_model("optional"), reasoning={"supported_efforts": ["xhigh", "high"]}),
        "unsupported": replace(_model("unsupported"), supported_parameters=("tools",)),
    })
    page = ModelsPage(AppSettings(provider="openrouter", default_model="required", default_thinking="off"))
    pane = LeftPane(None)
    pane.populate_models("openrouter")
    editor = AgentEditor(ModelChoices(targets=(ModelTargetChoice("openrouter", "required", "Required"),)))
    try:
        for owner, model_combo, thinking in (
            (page, page._model_combo, page._thinking_combo),
            (pane, pane._production_model_combo, pane._production_thinking_combo),
        ):
            for mid, expected in (("optional", ["off", "high", "max"]), ("required", ["high"]),
                                  ("unsupported", ["off"]), ("optional", ["off", "high", "max"])):
                model_combo.setCurrentIndex(model_combo.findData(mid))
                assert [thinking.itemData(i) for i in range(thinking.count())] == expected
                assert thinking.currentData() in expected
                if mid == "required":
                    assert "required" in thinking.currentText()
            if owner is pane:
                pane.set_production_model("required")
                pane.set_production_thinking("off")
                assert pane.current_production_thinking() == "high"
        editor.model.setCurrentIndex(1)
        assert [editor.thinking.itemData(i) for i in range(editor.thinking.count())] == ["inherit", "high"]
        assert "required" in editor.thinking.itemText(1)
    finally:
        page.cleanup_threads()
        page.deleteLater()
        pane.deleteLater()
        editor.deleteLater()
