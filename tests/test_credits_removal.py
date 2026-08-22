"""Focused tests verifying Aura Credits have been cleanly removed.

Covers:
1. provider registry does not contain "aura"
2. Aura Fast / Aura Pro models are unavailable
3. old settings selecting "aura" provider load safely (migrate to default)
4. obsolete pending claim fields do not affect startup
5. no Credits status-bar widget or click signal exists
6. no Credits popout is constructed
7. no checkout or claim endpoint can be called from the GUI
8. the no-provider send guard directs users to API Key Settings only
9. first-run setup directs users to provider settings only
10. all normal BYOK providers still populate and work
11. estimated per-session model cost remains intact
12. old settings with aura_pending values load without errors
13. the normal application enters production mode successfully
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from aura.config import cost_usd
from aura.gui.status_bar import AuraStatusBar
from aura.providers.registry import provider_registry

# ---------------------------------------------------------------------------
# 1.  Provider registry does not contain "aura"
# ---------------------------------------------------------------------------

class TestProviderRegistry:
    def test_aura_provider_not_in_registry(self) -> None:
        assert not provider_registry.has("aura")

    def test_aura_provider_not_in_ids(self) -> None:
        ids = provider_registry.ids()
        assert "aura" not in ids

    def test_only_known_providers_present(self) -> None:
        ids = provider_registry.ids()
        for pid in ids:
            assert pid != "aura", f"Removed provider {pid} should not be in registry"


# ---------------------------------------------------------------------------
# 2.  Aura Fast / Aura Pro not available
# ---------------------------------------------------------------------------

class TestAuraModelsRemoved:
    def test_aura_fast_not_in_any_provider(self) -> None:
        for pid, spec in provider_registry.all().items():
            assert "aura-fast" not in spec.models, f"aura-fast found in {pid}"

    def test_aura_pro_not_in_any_provider(self) -> None:
        for pid, spec in provider_registry.all().items():
            assert "aura-pro" not in spec.models, f"aura-pro found in {pid}"


# ---------------------------------------------------------------------------
# 3.  Old settings with "aura" provider migrate silently
# ---------------------------------------------------------------------------

class TestOldSettingsMigration:
    def test_aura_provider_migrates_to_default(self, tmp_path, monkeypatch) -> None:
        profile = tmp_path / "profile"
        profile.mkdir()
        config = {
            "provider": "aura",
            "default_model": "deepseek-v4-flash",
            "first_launch_done": True,
            "restore_last_conversation": False,
        }
        (profile / "config.json").write_text(json.dumps(config), encoding="utf-8")

        monkeypatch.setenv("AURA_CONFIG_DIR", str(profile))
        monkeypatch.setenv("AURA_DATA_DIR", str(profile))
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        import aura.paths
        monkeypatch.setattr(aura.paths, "config_dir", lambda: profile)
        monkeypatch.setattr(aura.paths, "data_dir", lambda: profile)

        import aura.key_manager
        from aura.config import has_usable_provider_configuration
        from aura.settings import load_settings

        monkeypatch.setattr(aura.key_manager, "_key_manager", None)
        settings = load_settings()
        assert settings.provider != "aura", "aura provider should be migrated"
        assert settings.provider in provider_registry.ids(), "migrated to a valid provider"
        assert has_usable_provider_configuration(settings.provider) is False


# ---------------------------------------------------------------------------
# 4.  Obsolete pending claim fields do not affect startup
# ---------------------------------------------------------------------------

class TestObsoleteFields:
    def test_pending_claim_fields_ignored(self, tmp_path, monkeypatch) -> None:
        """Settings with aura_pending_session_id and aura_pending_claim_secret load
        without error and the values are simply not part of the dataclass."""
        profile = tmp_path / "profile"
        profile.mkdir()
        config = {
            "provider": "deepseek",
            "aura_pending_session_id": "sess_abc123",
            "aura_pending_claim_secret": "secret_xyz",
            "first_launch_done": True,
            "restore_last_conversation": False,
        }
        (profile / "config.json").write_text(json.dumps(config), encoding="utf-8")

        monkeypatch.setenv("AURA_CONFIG_DIR", str(profile))
        monkeypatch.setenv("AURA_DATA_DIR", str(profile))

        import aura.paths
        monkeypatch.setattr(aura.paths, "config_dir", lambda: profile)
        monkeypatch.setattr(aura.paths, "data_dir", lambda: profile)

        from aura.settings import load_settings
        settings = load_settings()
        assert not hasattr(settings, "aura_pending_session_id"), "field removed from dataclass"
        assert not hasattr(settings, "aura_pending_claim_secret"), "field removed from dataclass"


# ---------------------------------------------------------------------------
# 5.  No Credits status-bar widget or click signal exists
# ---------------------------------------------------------------------------

class TestStatusBarNoCredits:
    def test_status_bar_has_no_credits_chip(self) -> None:
        """AuraStatusBar should not have a _status_balance or credits_chip_clicked signal."""
        _app = QApplication.instance() or QApplication([])
        sb = AuraStatusBar()
        assert not hasattr(sb, "credits_chip_clicked"), "credits_chip_clicked signal should not exist"
        assert not hasattr(sb, "_status_balance"), "_status_balance widget should not exist"


# ---------------------------------------------------------------------------
# 6.  No Credits popout module exists
# ---------------------------------------------------------------------------

class TestCreditsModulesDeleted:
    def test_credits_popout_module_missing(self) -> None:
        with pytest.raises(ImportError):
            import aura.gui.credits_popout  # noqa: F401

    def test_credits_panel_module_missing(self) -> None:
        with pytest.raises(ImportError):
            import aura.gui.credits_panel  # noqa: F401

    def test_balance_fetcher_module_missing(self) -> None:
        with pytest.raises(ImportError):
            import aura.gui.balance_fetcher  # noqa: F401

    def test_main_window_balance_module_missing(self) -> None:
        with pytest.raises(ImportError):
            import aura.gui.main_window_balance  # noqa: F401

    def test_aura_page_module_missing(self) -> None:
        with pytest.raises(ImportError):
            import aura.gui.settings_pages.aura_page  # noqa: F401

    def test_hosted_debug_report_modules_missing(self) -> None:
        with pytest.raises(ImportError):
            import aura.gui.debug_report_handler  # noqa: F401


# ---------------------------------------------------------------------------
# 8.  No stale lifecycle or hosted API references remain
# ---------------------------------------------------------------------------

class TestNoStaleLifecycleReferences:
    def test_runtime_sources_do_not_reference_deleted_credits_objects(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        forbidden = (
            "_balance_controller",
            "AuraCreditsPanel",
            "credits_popout",
            "balance_fetcher",
            "main_window_balance",
            "settings_pages.aura_page",
            "aura-fast",
            "aura-pro",
            "sweet-manifestation-production",
            "up.railway.app",
            'get_provider("aura")',
            "Add Credits",
        )
        for path in (repo_root / "aura").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in source, f"{token!r} remains in {path.relative_to(repo_root)}"

    def test_main_window_has_no_deleted_debug_report_or_balance_controller(self) -> None:
        from aura.gui.main_window import MainWindow

        source = Path(inspect.getsourcefile(MainWindow)).read_text(encoding="utf-8")
        assert "_balance_controller" not in source
        assert "_debug_report_handler" not in source

    def test_both_stop_controls_use_send_handler_cancellation(self) -> None:
        from aura.gui.main_window_signal_wiring import MainWindowSignalWiring

        source = inspect.getsource(MainWindowSignalWiring)
        assert (
            "w._input.stop_requested.connect(w._send_handler.handle_stop)" in source
        )
        assert (
            "w._playground.stop_execution_requested.connect(w._send_handler.handle_stop)"
            in source
        )
        assert "stop_execution_requested.connect(w._bridge.request_cancel)" not in source


# ---------------------------------------------------------------------------
# 9.  No-provider send guard copy
# ---------------------------------------------------------------------------

class TestSendGuardCopy:
    def test_send_handler_source_no_credits(self) -> None:
        """The source of SendHandler should not contain Aura Credits references."""
        from aura.gui.send_handler import SendHandler
        full_source = inspect.getsource(SendHandler)
        assert "Aura Credits" not in full_source
        assert "buy credits" not in full_source
        assert "Set up Aura Credits" not in full_source


# ---------------------------------------------------------------------------
# 9.  First-run setup copy
# ---------------------------------------------------------------------------

class TestSetupDialogCopy:
    def test_setup_dialog_mentions_api_keys(self) -> None:
        _app = QApplication.instance() or QApplication([])
        from aura.gui.setup_dialog import SetupDialog
        dlg = SetupDialog()
        title = dlg.windowTitle()
        assert "Credits" not in title
        from PySide6.QtWidgets import QLabel
        labels = dlg.findChildren(QLabel)
        text = " ".join(l.text() for l in labels)
        assert "API Key" in text or "API Keys" in text, "Setup dialog should mention API Keys"
        assert "Credits" not in text, "Setup dialog should not mention Credits"


# ---------------------------------------------------------------------------
# 10.  BYOK providers still populate and work
# ---------------------------------------------------------------------------

class TestByokProviders:
    def test_deepseek_present(self) -> None:
        assert provider_registry.has("deepseek")

    def test_openai_present(self) -> None:
        assert provider_registry.has("openai")

    def test_anthropic_present(self) -> None:
        assert provider_registry.has("anthropic")

    def test_google_cloud_present(self) -> None:
        assert provider_registry.has("google_cloud")

    def test_openrouter_present(self) -> None:
        assert provider_registry.has("openrouter")

    def test_deepseek_has_models(self) -> None:
        spec = provider_registry.get("deepseek")
        assert len(spec.models) > 0

    def test_openai_has_models(self) -> None:
        spec = provider_registry.get("openai")
        assert len(spec.models) > 0

    def test_all_providers_have_default_model(self) -> None:
        for pid, spec in provider_registry.all().items():
            assert spec.default_model, f"{pid} is missing default_model"


# ---------------------------------------------------------------------------
# 11.  Session cost estimation remains intact
# ---------------------------------------------------------------------------

class TestSessionCost:
    def test_cost_usd_works(self, monkeypatch) -> None:
        # cost_usd(model, cache_hit_tokens, cache_miss_tokens, output_tokens)
        # DeepSeek rates come from the pricing source store — seed a fetched
        # result (off-peak UTC hour) and verify the cost math consumes it.
        from aura.providers import pricing as pricing_mod
        from aura.providers.pricing import ModelRates, PeakWindow, PricingResult, RateTier

        saved = dict(pricing_mod._results)
        pricing_mod._results.clear()
        try:
            monkeypatch.setattr(
                pricing_mod,
                "_utcnow",
                lambda: datetime(2025, 1, 1, 5, 0, tzinfo=timezone.utc),
            )
            pricing_mod._results["deepseek"] = PricingResult(
                provider_id="deepseek",
                models={
                    "deepseek-v4-flash": ModelRates(
                        model_id="deepseek-v4-flash",
                        off_peak=RateTier(in_miss=0.22, in_hit=0.007, out=0.66),
                        peak=RateTier(in_miss=0.44, in_hit=0.014, out=1.32),
                    ),
                },
                source_url="https://api-docs.deepseek.com/quick_start/pricing/",
                retrieved_at="2025-01-01T00:00:00+00:00",
                peak_windows=(PeakWindow(start_minute=60, end_minute=240),),
            )
            cost = cost_usd("deepseek-v4-flash", 500, 1000, 800)
            assert cost is not None
            assert cost > 0
            expected = (500 * 0.007 + 1000 * 0.22 + 800 * 0.66) / 1_000_000
            assert abs(cost - expected) < 1e-10
        finally:
            pricing_mod._results.clear()
            pricing_mod._results.update(saved)

    def test_cost_usd_for_unknown_model(self) -> None:
        cost = cost_usd("nonexistent-model", 0, 0, 0)
        assert cost is None


# ---------------------------------------------------------------------------
# 12.  Normal application enters production mode successfully
# ---------------------------------------------------------------------------

class TestProductionModeEntry:
    def test_production_provider_selection_works(self, tmp_path, monkeypatch) -> None:
        """Construct MainWindow with a valid config; it should enter production mode."""
        _app = QApplication.instance() or QApplication([])
        profile = tmp_path / "profile"
        profile.mkdir()
        config = {
            "provider": "deepseek",
            "default_model": "deepseek-v4-flash",
            "default_thinking": "high",
            "first_launch_done": True,
            "restore_last_conversation": False,
        }
        (profile / "config.json").write_text(json.dumps(config), encoding="utf-8")

        monkeypatch.setenv("AURA_CONFIG_DIR", str(profile))
        monkeypatch.setenv("AURA_DATA_DIR", str(profile))

        import aura.paths
        monkeypatch.setattr(aura.paths, "config_dir", lambda: profile)
        monkeypatch.setattr(aura.paths, "data_dir", lambda: profile)

        # This window hydrates provider pricing at startup; keep this
        # offline test off the network.
        from aura.gui.main_window_pricing import MainWindowPricingController
        monkeypatch.setattr(
            MainWindowPricingController,
            "schedule_startup_refresh",
            lambda self, delay_ms=0: False,
        )

        from aura.gui.main_window import MainWindow
        window = MainWindow()
        try:
            assert window._settings.provider == "deepseek"
            assert window._settings.default_model == "deepseek-v4-flash"
            assert window._settings.default_thinking == "high"
        finally:
            window.close()
            window.deleteLater()
