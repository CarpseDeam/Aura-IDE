"""Focused GUI coverage for Local Model setup and discovery."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.gui import settings_dialog as settings_dialog_module  # noqa: E402
from aura.gui.settings_dialog import SettingsDialog  # noqa: E402
from aura.gui.settings_pages import models_page as models_page_module  # noqa: E402
from aura.gui.settings_pages.models_page import ModelsPage  # noqa: E402
from aura.providers.base import ModelInfo  # noqa: E402
from aura.providers.registry import provider_registry  # noqa: E402
from aura.settings import AppSettings  # noqa: E402

LOCAL_PROVIDER = "local_openai"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def local_catalog() -> Iterator[object]:
    cfg = provider_registry.get(LOCAL_PROVIDER)
    models_before = dict(cfg.models)
    pricing_before = dict(cfg.pricing)
    base_url_before = cfg.base_url
    yield cfg
    cfg.models.clear()
    cfg.models.update(models_before)
    cfg.pricing.clear()
    cfg.pricing.update(pricing_before)
    cfg.base_url = base_url_before


def _model(model_id: str) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        label=model_id,
        input_per_m_usd=0.0,
        output_per_m_usd=0.0,
        cache_hit_per_m_usd=0.0,
    )


def _select_provider(page: ModelsPage, provider_id: str) -> None:
    index = page._provider_combo.findData(provider_id)
    assert index >= 0
    page._provider_combo.setCurrentIndex(index)


def test_local_controls_are_conditional_and_selection_does_not_connect(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    local_catalog: object,
) -> None:
    starts: list[tuple[str, str | None]] = []

    def record_start(self, provider_id, *, base_url=None):
        starts.append((provider_id, base_url))
        return True

    monkeypatch.setattr(ModelsPage, "_start_discovery", record_start)
    page = ModelsPage(AppSettings(provider="deepseek"))
    try:
        assert page._local_endpoint_row.isHidden()
        starts.clear()

        _select_provider(page, LOCAL_PROVIDER)

        assert not page._local_endpoint_row.isHidden()
        assert not page._local_status_label.isHidden()
        assert starts == []
        assert page._thinking_combo.currentData() == "off"
        assert not page._thinking_combo.isEnabled()

        _select_provider(page, "deepseek")
        assert page._local_endpoint_row.isHidden()
        assert page._thinking_combo.isEnabled()
        assert page._thinking_combo.currentData() == "high"

        page._local_endpoint_edit.setText("http://127.0.0.1:8080/v1")
        assert page.validation_error() is not None
    finally:
        page.cleanup_threads()
        page.deleteLater()


def test_local_discovery_uses_typed_endpoint_and_replaces_snapshot(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    local_catalog: object,
) -> None:
    cfg = provider_registry.get(LOCAL_PROVIDER)
    cfg.models.clear()
    cfg.models["old-model"] = _model("old-model")
    cfg.pricing.clear()

    saved: list[tuple[str, set[str]]] = []
    monkeypatch.setattr(ModelsPage, "_start_discovery", lambda *a, **k: True)
    monkeypatch.setattr(
        models_page_module,
        "save_dynamic_catalog",
        lambda provider_id, models, pricing: saved.append(
            (provider_id, set(models))
        ),
    )

    page = ModelsPage(
        AppSettings(
            provider=LOCAL_PROVIDER,
            default_model="old-model",
            local_openai_base_url="http://127.0.0.1:11434/v1",
        )
    )
    try:
        calls: list[tuple[str, str | None]] = []

        def start(provider_id, *, base_url=None):
            calls.append((provider_id, base_url))
            page._discovery_inflight.add(provider_id)
            page._discovery_base_urls[provider_id] = base_url
            return True

        page._start_discovery = start  # type: ignore[method-assign]
        page._local_endpoint_edit.setText(" http://127.0.0.1:1234/v1/ ")

        assert page.validation_error() is not None
        page._on_local_discover()
        assert calls == [(LOCAL_PROVIDER, "http://127.0.0.1:1234/v1")]
        assert not page._local_discover_btn.isEnabled()

        page._on_discovery_finished(
            LOCAL_PROVIDER,
            {"new-a": _model("new-a"), "new-b": _model("new-b")},
            {
                "new-a": {"in_miss": 0.0, "in_hit": 0.0, "out": 0.0},
                "new-b": {"in_miss": 0.0, "in_hit": 0.0, "out": 0.0},
            },
            "",
        )
        # The real QThread emits ``finished`` immediately after the worker's
        # result, which clears the in-flight guard before the user can apply.
        page._discovery_inflight.discard(LOCAL_PROVIDER)

        assert set(cfg.models) == {"new-a", "new-b"}
        assert page._model_combo.currentData() == "new-a"
        assert page.validation_error() is None
        assert saved == []
        assert "2 models discovered" in page._local_status_label.text()

        result = AppSettings()
        page.collect_settings(result)
        assert result.provider == LOCAL_PROVIDER
        assert result.default_model == "new-a"
        assert result.local_openai_base_url == "http://127.0.0.1:1234/v1"
        page.commit_changes()
        assert saved == [(LOCAL_PROVIDER, {"new-a", "new-b"})]
    finally:
        page.cleanup_threads()
        page.deleteLater()


def test_failed_test_keeps_cached_models_and_changed_endpoint_invalid(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    local_catalog: object,
) -> None:
    cfg = provider_registry.get(LOCAL_PROVIDER)
    cfg.models.clear()
    cfg.models["cached"] = _model("cached")
    cfg.pricing.clear()
    monkeypatch.setattr(ModelsPage, "_start_discovery", lambda *a, **k: True)

    page = ModelsPage(
        AppSettings(
            provider=LOCAL_PROVIDER,
            default_model="cached",
            local_openai_base_url="http://127.0.0.1:11434/v1",
        )
    )
    try:
        page._local_endpoint_edit.setText("http://127.0.0.1:8080/v1")
        page._discovery_base_urls[LOCAL_PROVIDER] = page._local_endpoint_edit.text()
        page._on_discovery_finished(
            LOCAL_PROVIDER,
            {},
            {},
            "connection refused",
        )

        assert set(cfg.models) == {"cached"}
        assert page._model_combo.currentData() == "cached"
        assert page.validation_error() is not None
        assert "Keeping 1 cached model" in page._local_status_label.text()
    finally:
        page.cleanup_threads()
        page.deleteLater()


def test_discard_restores_the_catalog_from_before_discovery(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    local_catalog: object,
) -> None:
    cfg = provider_registry.get(LOCAL_PROVIDER)
    cfg.models.clear()
    cfg.models["before"] = _model("before")
    cfg.pricing.clear()
    cfg.pricing["before"] = {"in_miss": 0.0, "in_hit": 0.0, "out": 0.0}
    monkeypatch.setattr(ModelsPage, "_start_discovery", lambda *a, **k: True)
    monkeypatch.setattr(
        models_page_module,
        "save_dynamic_catalog",
        lambda *args, **kwargs: pytest.fail("Cancel must not write the cache"),
    )

    page = ModelsPage(
        AppSettings(
            provider=LOCAL_PROVIDER,
            default_model="before",
            local_openai_base_url="http://127.0.0.1:11434/v1",
        )
    )
    try:
        page._discovery_base_urls[LOCAL_PROVIDER] = (
            "http://127.0.0.1:11434/v1"
        )
        page._on_discovery_finished(
            LOCAL_PROVIDER,
            {"provisional": _model("provisional")},
            {
                "provisional": {
                    "in_miss": 0.0,
                    "in_hit": 0.0,
                    "out": 0.0,
                }
            },
            "",
        )
        assert set(cfg.models) == {"provisional"}

        page.discard_changes()

        assert set(cfg.models) == {"before"}
        assert set(cfg.pricing) == {"before"}
    finally:
        page.cleanup_threads()
        page.deleteLater()


def test_validation_waits_for_inflight_local_discovery(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    local_catalog: object,
) -> None:
    cfg = provider_registry.get(LOCAL_PROVIDER)
    cfg.models.clear()
    cfg.models["cached"] = _model("cached")
    cfg.pricing.clear()
    monkeypatch.setattr(ModelsPage, "_start_discovery", lambda *a, **k: False)

    page = ModelsPage(
        AppSettings(
            provider=LOCAL_PROVIDER,
            default_model="cached",
            local_openai_base_url="http://127.0.0.1:11434/v1",
        )
    )
    try:
        assert page.validation_error() is None

        page._discovery_inflight.add(LOCAL_PROVIDER)

        assert page.validation_error() == (
            "Wait for Test / Discover to finish before applying."
        )
    finally:
        page._discovery_inflight.discard(LOCAL_PROVIDER)
        page.cleanup_threads()
        page.deleteLater()


def test_settings_dialog_refuses_invalid_local_selection(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ModelsPage, "_start_discovery", lambda *a, **k: False)
    saved: list[AppSettings] = []
    warnings: list[str] = []
    monkeypatch.setattr(settings_dialog_module, "save_settings", saved.append)
    monkeypatch.setattr(
        settings_dialog_module.QMessageBox,
        "warning",
        lambda parent, title, message: warnings.append(message),
    )

    dialog = SettingsDialog(
        settings=AppSettings(),
        workspace_root=None,
        on_change_root=lambda: None,
    )
    try:
        dialog._models_page.validation_error = (  # type: ignore[method-assign]
            lambda: "Test / Discover the current local model endpoint before applying."
        )

        dialog.apply()
        dialog.accept()

        assert not saved
        assert len(warnings) == 2
        assert dialog.result() != SettingsDialog.DialogCode.Accepted
        assert dialog._tabs.currentIndex() == 0
    finally:
        dialog.reject()
        dialog.deleteLater()
