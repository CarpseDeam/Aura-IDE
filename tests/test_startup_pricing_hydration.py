"""Focused tests for startup pricing hydration.

A normal launch must hydrate the selected provider's official rates once,
off the UI thread, without the user opening Settings -> Models. These tests
cover the real startup owner (MainWindow's pricing controller), a fresh
profile with no cache, a failed refresh preserving the last-known-good
result, a provider with no pricing source, thread ownership across
shutdown, and the resulting numeric cost on the telemetry/footer path.

Every test stubs the network — nothing here fetches a live pricing page.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QEventLoop, QThread  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from aura.conversation.telemetry import ConversationTelemetry  # noqa: E402
from aura.gui import main_window_pricing  # noqa: E402
from aura.gui.main_window_pricing import MainWindowPricingController  # noqa: E402
from aura.providers import pricing  # noqa: E402
from aura.providers.pricing import pricing_cache_path, rates_for  # noqa: E402
from aura.providers.pricing_sources.deepseek import DeepSeekPricingSource  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "deepseek_pricing"

OFF_PEAK_UTC = datetime(2025, 1, 1, 5, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def pricing_store_clean():
    """Snapshot and restore the global pricing store around every test."""
    saved = dict(pricing._results)
    pricing._results.clear()
    yield
    pricing._results.clear()
    pricing._results.update(saved)
    main_window_pricing._LINGERING_THREADS.clear()


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    """Point the last-known-good pricing cache at a per-test directory."""
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("AURA_CONFIG_DIR", str(profile))
    monkeypatch.setenv("AURA_DATA_DIR", str(profile))
    return profile


def _standard_result() -> pricing.PricingResult:
    result = DeepSeekPricingSource().parse(
        (FIXTURES / "pricing_standard.html").read_text(encoding="utf-8")
    )
    assert result is not None
    return result


def _pump(qapp, predicate, timeout_s: float = 10.0) -> bool:
    """Spin the Qt event loop until *predicate* holds or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.005)
    return predicate()


def _run_startup_refresh(qapp, controller) -> tuple[str, bool]:
    """Schedule the startup refresh and pump until it reports back."""
    done: list[tuple[str, bool]] = []
    controller.refreshFinished.connect(lambda pid, priced: done.append((pid, priced)))
    assert controller.schedule_startup_refresh() is True
    assert _pump(qapp, lambda: bool(done)), "startup pricing refresh never finished"
    assert _pump(qapp, lambda: controller._thread is None), "refresh thread was never cleared"
    return done[0]


# ---------------------------------------------------------------------------
# 1. The real normal-startup owner schedules exactly one refresh
# ---------------------------------------------------------------------------


class TestNormalStartupOwner:
    def test_main_window_startup_refreshes_pricing_once_without_settings(
        self, qapp, tmp_path, monkeypatch
    ) -> None:
        """Constructing the ordinary main window — no Settings dialog and no
        model discovery — must trigger exactly one pricing refresh for the
        selected provider."""
        profile = tmp_path / "profile"
        profile.mkdir()
        config = {
            "provider": "deepseek",
            "default_model": "deepseek-v4-flash",
            "first_launch_done": True,
            "restore_last_conversation": False,
        }
        (profile / "config.json").write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setenv("AURA_CONFIG_DIR", str(profile))
        monkeypatch.setenv("AURA_DATA_DIR", str(profile))

        import aura.paths
        monkeypatch.setattr(aura.paths, "config_dir", lambda: profile)
        monkeypatch.setattr(aura.paths, "data_dir", lambda: profile)

        refreshed: list[str] = []

        def _record(provider_id):
            refreshed.append(provider_id)
            return _standard_result()

        monkeypatch.setattr(main_window_pricing, "refresh_provider_pricing", _record)

        # Model discovery (the Settings -> Models path) must not run.
        from aura.gui.settings_pages import models_page

        def _no_discovery(provider_id):
            raise AssertionError("startup must not run provider model discovery")

        monkeypatch.setattr(models_page, "fetch_provider_models", _no_discovery)

        # Keep the unrelated background update check off the network.
        from aura.gui.main_window_update import MainWindowUpdateController

        monkeypatch.setattr(MainWindowUpdateController, "check_for_updates", lambda self: None)

        from aura.gui.main_window import MainWindow

        window = MainWindow()
        try:
            controller = window._pricing_controller
            done: list[tuple[str, bool]] = []
            controller.refreshFinished.connect(lambda pid, ok: done.append((pid, ok)))
            assert _pump(qapp, lambda: bool(done)), "startup never refreshed pricing"
            assert refreshed == ["deepseek"]
            assert done == [("deepseek", True)]

            # Once per startup only — no second refresh is ever scheduled.
            assert controller.schedule_startup_refresh() is False
            assert _pump(qapp, lambda: controller._thread is None)
            assert refreshed == ["deepseek"]
        finally:
            window.close()
            window.deleteLater()


# ---------------------------------------------------------------------------
# 2. Fresh profile: fetched result is stored and persisted
# ---------------------------------------------------------------------------


class TestFreshProfileHydration:
    def test_startup_refresh_populates_store_and_cache(
        self, qapp, monkeypatch, isolated_config_dir
    ) -> None:
        fetched = _standard_result()
        monkeypatch.setattr(DeepSeekPricingSource, "fetch", lambda self: fetched)
        assert not pricing_cache_path().exists()

        controller = MainWindowPricingController("deepseek")
        assert _run_startup_refresh(qapp, controller) == ("deepseek", True)

        assert rates_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC) == {
            "in_miss": 0.22,
            "in_hit": 0.007,
            "out": 0.66,
        }
        path = pricing_cache_path()
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["deepseek"]["schema_version"] == 2
        assert data["deepseek"]["models"]["deepseek-v4-flash"]["off_peak"]["in_miss"] == 0.22


# ---------------------------------------------------------------------------
# 3. A failed startup refresh preserves the last-known-good result
# ---------------------------------------------------------------------------


class TestFailedStartupRefresh:
    def test_failure_preserves_valid_cached_result(
        self, qapp, monkeypatch, isolated_config_dir
    ) -> None:
        good = _standard_result()
        pricing._results["deepseek"] = good
        pricing._save_cache(good)
        cached_bytes = pricing_cache_path().read_bytes()
        pricing._results.clear()  # a fresh process, cache still on disk

        monkeypatch.setattr(DeepSeekPricingSource, "fetch", lambda self: None)

        controller = MainWindowPricingController("deepseek")
        provider_id, priced = _run_startup_refresh(qapp, controller)
        assert provider_id == "deepseek"
        assert priced is True  # the last-known-good result is still in force

        assert rates_for("deepseek", "deepseek-v4-flash", OFF_PEAK_UTC) == {
            "in_miss": 0.22,
            "in_hit": 0.007,
            "out": 0.66,
        }
        assert pricing_cache_path().read_bytes() == cached_bytes

    def test_raising_fetch_does_not_disturb_in_memory_result(
        self, qapp, monkeypatch, isolated_config_dir
    ) -> None:
        good = _standard_result()
        pricing._results["deepseek"] = good

        def _boom(self):
            raise RuntimeError("network down")

        monkeypatch.setattr(DeepSeekPricingSource, "fetch", _boom)

        controller = MainWindowPricingController("deepseek")
        assert _run_startup_refresh(qapp, controller) == ("deepseek", True)
        assert pricing._results["deepseek"] is good


# ---------------------------------------------------------------------------
# 4. A provider with no pricing source is a harmless no-op
# ---------------------------------------------------------------------------


class TestUnsourcedProvider:
    def test_provider_without_source_never_schedules_a_refresh(self, qapp, monkeypatch) -> None:
        def _never(provider_id):
            raise AssertionError("unsourced provider must not refresh pricing")

        monkeypatch.setattr(main_window_pricing, "refresh_provider_pricing", _never)

        controller = MainWindowPricingController("openrouter")
        assert controller.schedule_startup_refresh() is False
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        assert controller._thread is None
        controller.shutdown()  # still safe with nothing running

    def test_empty_provider_id_is_a_no_op(self, qapp) -> None:
        controller = MainWindowPricingController("")
        assert controller.schedule_startup_refresh() is False
        assert controller._thread is None


# ---------------------------------------------------------------------------
# 5. Thread ownership: off the UI thread, and joined at shutdown
# ---------------------------------------------------------------------------


class TestThreadOwnership:
    def test_refresh_runs_off_the_ui_thread(self, qapp, monkeypatch) -> None:
        ran_in: list[QThread] = []

        def _record(provider_id):
            ran_in.append(QThread.currentThread())
            return _standard_result()

        monkeypatch.setattr(main_window_pricing, "refresh_provider_pricing", _record)

        controller = MainWindowPricingController("deepseek")
        assert _run_startup_refresh(qapp, controller) == ("deepseek", True)

        assert len(ran_in) == 1
        assert ran_in[0] is not qapp.thread()
        assert controller._thread is None
        assert controller._worker is None

    def test_shutdown_leaves_no_running_thread(self, qapp, monkeypatch) -> None:
        started = threading.Event()
        release = threading.Event()

        def _blocking(provider_id):
            started.set()
            release.wait(10.0)
            return _standard_result()

        monkeypatch.setattr(main_window_pricing, "refresh_provider_pricing", _blocking)

        controller = MainWindowPricingController("deepseek")
        assert controller.schedule_startup_refresh() is True
        assert _pump(qapp, started.is_set), "refresh worker never started"

        thread = controller._thread
        assert thread is not None
        assert thread.isRunning()

        release.set()
        controller.shutdown()

        assert not thread.isRunning()
        assert controller._thread is None
        assert controller._worker is None
        assert main_window_pricing._LINGERING_THREADS == []


# ---------------------------------------------------------------------------
# 6. Usage recorded after hydration is priced numerically
# ---------------------------------------------------------------------------


class TestPricedUsageAfterHydration:
    def test_usage_after_startup_hydration_has_numeric_cost(
        self, qapp, monkeypatch, isolated_config_dir
    ) -> None:
        fetched = _standard_result()
        monkeypatch.setattr(DeepSeekPricingSource, "fetch", lambda self: fetched)

        controller = MainWindowPricingController("deepseek")
        assert _run_startup_refresh(qapp, controller) == ("deepseek", True)

        telemetry = ConversationTelemetry()
        telemetry.record_usage(
            model_id="deepseek-v4-flash",
            prompt=1_500,
            completion=800,
            hit=500,
            miss=1_000,
            context_window_tokens=1_000_000,
            now=OFF_PEAK_UTC,
        )

        event = telemetry.events[-1]
        assert event.provider_id == "deepseek"
        assert event.pricing_tier == "off_peak"
        cost = event.cost_decimal()
        assert cost is not None
        expected = (500 * 0.007 + 1000 * 0.22 + 800 * 0.66) / 1_000_000
        assert abs(float(cost) - expected) < 1e-12

        summary = telemetry.cost_summary()
        assert summary.unknown_count == 0
        assert summary.known_total is not None

        from aura.gui.status_bar import AuraStatusBar

        bar = AuraStatusBar()
        try:
            bar.refresh(
                workspace_root=str(isolated_config_dir),
                model_id="deepseek-v4-flash",
                thinking="off",
                conversation_usage=telemetry.per_model,
                telemetry=telemetry,
            )
            text = bar._status_session.text()
            assert "$" in text
        finally:
            bar.deleteLater()
