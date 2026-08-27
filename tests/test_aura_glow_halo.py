"""Synchronization, lifecycle, and fixed-geometry rendering checks for Aura halos."""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import (  # noqa: E402
    QAbstractAnimation,
    QCoreApplication,
    QEvent,
    QPoint,
    QVariantAnimation,
)
from PySide6.QtGui import QImage, QRegion  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from aura.gui.chat_view import ChatView  # noqa: E402
from aura.gui.widgets.aura_glow import AuraPhaseDriver, AuraWidget  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _make_driver(qapp) -> AuraPhaseDriver:
    return AuraPhaseDriver(qapp)


def _make_widget(
    qapp,
    *,
    driver: AuraPhaseDriver | None = None,
    w: int = 500,
    h: int = 150,
    inner_w: int = 400,
    inner_h: int = 60,
    spread: int = 16,
) -> AuraWidget:
    inner = QLabel("card")
    inner.setFixedSize(inner_w, inner_h)
    widget = AuraWidget(
        inner,
        phase_driver=driver or _make_driver(qapp),
        glow_spread=spread,
    )
    widget.resize(w, h)
    return widget


def _finish_fade(widget: AuraWidget) -> None:
    assert widget._envelope_anim is not None
    widget._envelope_anim.setCurrentTime(widget._FADE_DURATION_MS)


def _set_master_time(driver: AuraPhaseDriver, milliseconds: int) -> None:
    driver._animation.setCurrentTime(milliseconds)


def _render(widget: AuraWidget) -> QImage:
    image = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    widget.render(image)
    return image


def _render_alpha(widget: AuraWidget, x: int, y: int) -> int:
    return _render(widget).pixelColor(x, y).alpha()


def test_two_widgets_share_identical_breath_and_rainbow_at_each_phase(qapp) -> None:
    driver = _make_driver(qapp)
    first = _make_widget(qapp, driver=driver)
    second = _make_widget(qapp, driver=driver)
    first.start_aura()
    second.start_aura()

    for master_time in (0, 800, 1600, 3199, 4800, 9600, 12799):
        _set_master_time(driver, master_time)
        assert first._breath == pytest.approx(second._breath)
        assert first._breath == pytest.approx(driver.breath)
        assert first._glow_color.rgba() == second._glow_color.rgba()
        assert first._glow_color.rgba() == driver.color.rgba()


def test_master_clock_rotates_hue_across_exactly_four_breaths(qapp) -> None:
    driver = _make_driver(qapp)
    widget = _make_widget(qapp, driver=driver)
    widget.start_aura()

    assert driver._animation.duration() == 12_800
    assert driver.HUE_DURATION_MS == 4 * widget._BREATH_DURATION_MS

    _set_master_time(driver, 1600)
    assert driver.phase == pytest.approx(0.125)
    assert widget._breath == pytest.approx(1.0)
    first_peak_color = widget._glow_color.rgba()

    _set_master_time(driver, 4800)
    assert driver.phase == pytest.approx(0.375)
    assert widget._breath == pytest.approx(1.0)
    assert widget._glow_color.rgba() != first_peak_color


def test_late_join_samples_current_phase_without_restarting_driver(qapp) -> None:
    driver = _make_driver(qapp)
    first = _make_widget(qapp, driver=driver)
    first.start_aura()
    _set_master_time(driver, 4700)
    phase_before = driver.phase
    time_before = driver._animation.currentTime()

    second = _make_widget(qapp, driver=driver)
    second.start_aura()

    assert driver.phase == pytest.approx(phase_before)
    assert driver._animation.currentTime() == time_before
    assert second._breath == pytest.approx(first._breath)
    assert second._glow_color.rgba() == first._glow_color.rgba()
    assert driver.active_widget_count == 2


def test_widgets_keep_independent_fade_envelopes_on_one_phase(qapp) -> None:
    driver = _make_driver(qapp)
    first = _make_widget(qapp, driver=driver)
    second = _make_widget(qapp, driver=driver)
    first.start_aura()
    _finish_fade(first)
    second.start_aura()
    assert second._envelope_anim is not None
    second._envelope_anim.setCurrentTime(second._FADE_DURATION_MS // 2)
    _set_master_time(driver, 5100)

    assert first._envelope == pytest.approx(1.0)
    assert 0.0 < second._envelope < 1.0
    assert first._breath == pytest.approx(second._breath)
    assert first._glow_color.rgba() == second._glow_color.rgba()

    first.stop_aura()
    assert first._envelope_anim is not None
    first._envelope_anim.setCurrentTime(first._FADE_DURATION_MS // 2)
    assert 0.0 < first._envelope < 1.0
    assert 0.0 < second._envelope < 1.0


def test_stopping_one_widget_leaves_driver_running_for_the_other(qapp) -> None:
    driver = _make_driver(qapp)
    first = _make_widget(qapp, driver=driver)
    second = _make_widget(qapp, driver=driver)
    first.start_aura()
    second.start_aura()
    _finish_fade(first)
    _finish_fade(second)
    _set_master_time(driver, 6300)
    phase_before = driver.phase

    first.stop_aura()
    _finish_fade(first)

    assert driver.active_widget_count == 1
    assert not driver.is_registered(first)
    assert driver.is_registered(second)
    assert driver._animation.state() == QAbstractAnimation.State.Running
    assert driver.phase == pytest.approx(phase_before)


def test_driver_idles_after_final_widget_finishes_fading(qapp) -> None:
    driver = _make_driver(qapp)
    widget = _make_widget(qapp, driver=driver)
    widget.start_aura()
    _finish_fade(widget)
    _set_master_time(driver, 7300)
    phase_before = driver.phase

    widget.stop_aura()
    assert driver._animation.state() == QAbstractAnimation.State.Running
    _finish_fade(widget)

    assert driver.active_widget_count == 0
    assert driver._animation.state() == QAbstractAnimation.State.Paused
    assert driver.phase == pytest.approx(phase_before)


def test_repeated_start_stop_calls_do_not_duplicate_registrations(qapp) -> None:
    driver = _make_driver(qapp)
    widget = _make_widget(qapp, driver=driver)
    widget.start_aura()
    first_envelope_anim = widget._envelope_anim
    widget.start_aura()
    widget.start_aura()

    assert driver.active_widget_count == 1
    assert widget._envelope_anim is first_envelope_anim

    _finish_fade(widget)
    widget.stop_aura()
    fade_out = widget._envelope_anim
    widget.stop_aura()
    assert widget._envelope_anim is fade_out
    assert driver.active_widget_count == 1

    _finish_fade(widget)
    widget.stop_aura()
    assert driver.active_widget_count == 0


def test_deleted_widget_does_not_leave_stale_registration(qapp) -> None:
    driver = _make_driver(qapp)
    widget = _make_widget(qapp, driver=driver)
    widget.start_aura()
    assert driver.active_widget_count == 1

    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert driver.active_widget_count == 0
    assert driver._animation.state() == QAbstractAnimation.State.Paused


def test_cleared_chat_wrappers_do_not_leave_stale_registrations(qapp) -> None:
    driver = _make_driver(qapp)
    chat = ChatView(driver)
    chat.begin_assistant()
    assert driver.active_widget_count == 1

    chat.reset()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert driver.active_widget_count == 0
    assert driver._animation.state() == QAbstractAnimation.State.Paused


def test_no_aura_widget_owns_a_looping_breath_or_color_animation(qapp) -> None:
    driver = _make_driver(qapp)
    widget = _make_widget(qapp, driver=driver)
    widget.start_aura()

    assert not hasattr(widget, "_breath_anim")
    assert not hasattr(widget, "_color_anim")
    assert not hasattr(widget, "set_glow_state")
    assert not hasattr(widget, "transition_glow_color")
    assert all(
        animation.loopCount() != -1
        for animation in widget.findChildren(QVariantAnimation)
    )
    assert driver._animation.loopCount() == -1


def test_active_breath_never_fully_disappears_at_cycle_endpoints(qapp) -> None:
    driver = _make_driver(qapp)
    widget = _make_widget(qapp, driver=driver)
    widget.start_aura()
    _finish_fade(widget)

    for master_time in (0, 3200, 6400, 9600, 12800):
        _set_master_time(driver, master_time)
        assert widget._breath == pytest.approx(widget._BREATH_FLOOR, abs=1e-3)
        assert widget._breath > 0.0


def test_halo_alpha_capped_below_legacy_ceiling(qapp) -> None:
    driver = _make_driver(qapp)
    widget = _make_widget(qapp, driver=driver)
    widget.start_aura()
    _finish_fade(widget)
    _set_master_time(driver, widget._BREATH_DURATION_MS // 2)

    image = _render(widget)
    region = widget._cached_ring_region
    assert region is not None
    max_alpha = 0
    for x in range(0, widget.width(), 2):
        for y in range(0, widget.height(), 2):
            if region.contains(QPoint(x, y)):
                max_alpha = max(max_alpha, image.pixelColor(x, y).alpha())

    assert 0 < max_alpha < 240
    assert max_alpha <= widget._MAX_ALPHA + 1


def test_halo_geometry_fixed_across_shared_breath_cycle(qapp) -> None:
    driver = _make_driver(qapp)
    widget = _make_widget(qapp, driver=driver)
    widget.start_aura()
    _finish_fade(widget)

    _set_master_time(driver, 0)
    layers_a = [(path.boundingRect(), width) for path, width, _ in widget._cached_halo_layers]
    _set_master_time(driver, widget._BREATH_DURATION_MS // 2)
    layers_b = [(path.boundingRect(), width) for path, width, _ in widget._cached_halo_layers]

    assert layers_a == layers_b


class _UpdateRecordingAura(AuraWidget):
    def __init__(self, *args, **kwargs) -> None:
        self.update_calls: list[tuple] = []
        super().__init__(*args, **kwargs)

    def update(self, *args) -> None:
        self.update_calls.append(args)
        super().update(*args)


def test_phase_and_envelope_repaints_stay_scoped_to_ring_region(qapp) -> None:
    driver = _make_driver(qapp)
    inner = QLabel("card")
    inner.setFixedSize(400, 60)
    widget = _UpdateRecordingAura(inner, phase_driver=driver, glow_spread=16)
    widget.resize(500, 150)
    widget.show()
    qapp.processEvents()
    assert widget._cached_ring_region is not None
    widget.update_calls.clear()

    widget.start_aura()
    _set_master_time(driver, 1700)

    assert widget.update_calls
    ring_updates = [
        args for args in widget.update_calls
        if len(args) == 1 and isinstance(args[0], QRegion)
    ]
    assert len(ring_updates) >= 2
    assert not any(len(args) == 1 and not isinstance(args[0], QRegion) for args in widget.update_calls)


def test_idle_widget_paints_nothing(qapp) -> None:
    widget = _make_widget(qapp)
    assert _render_alpha(widget, widget.width() // 2, 2) == 0
    assert _render_alpha(widget, 2, widget.height() // 2) == 0


@pytest.mark.parametrize(
    "w, h, inner_w, inner_h",
    [
        (1400, 100, 1200, 40),
        (140, 900, 60, 700),
    ],
)
def test_halo_wraps_all_four_edges_not_just_vertical_bands(
    qapp, w, h, inner_w, inner_h
) -> None:
    driver = _make_driver(qapp)
    widget = _make_widget(
        qapp,
        driver=driver,
        w=w,
        h=h,
        inner_w=inner_w,
        inner_h=inner_h,
        spread=16,
    )
    widget.start_aura()
    _finish_fade(widget)
    _set_master_time(driver, widget._BREATH_DURATION_MS // 2)
    image = _render(widget)

    width, height = widget.width(), widget.height()
    band = max(1, widget._glow_spread // 2)
    edges = {
        "top": image.pixelColor(width // 2, band).alpha(),
        "bottom": image.pixelColor(width // 2, height - band).alpha(),
        "left": image.pixelColor(band, height // 2).alpha(),
        "right": image.pixelColor(width - band, height // 2).alpha(),
    }
    for name, alpha in edges.items():
        assert alpha > 0, f"expected halo on {name} edge, got alpha={alpha}"
    assert max(edges.values()) / min(edges.values()) < 4


def test_production_construction_injects_one_driver_into_both_halos(
    qapp, tmp_path, monkeypatch
) -> None:
    from aura.config import AppSettings
    from aura.gui.main_window import MainWindow

    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("AURA_CONFIG_DIR", str(profile))
    monkeypatch.setenv("AURA_DATA_DIR", str(profile))
    settings = AppSettings()
    settings.first_launch_done = True
    settings.restore_last_conversation = False
    monkeypatch.setattr("aura.gui.main_window.load_settings", lambda: settings)
    monkeypatch.setattr("aura.gui.main_window.load_workspace_root", lambda: None)
    monkeypatch.setattr("aura.gui.main_window.save_settings", lambda _settings: None)
    monkeypatch.setattr(
        "aura.gui.main_window_update.MainWindowUpdateController.check_for_updates",
        lambda _self: None,
    )
    monkeypatch.setattr(
        "aura.gui.main_window_pricing.MainWindowPricingController.schedule_startup_refresh",
        lambda _self, delay_ms=0: False,
    )

    window = MainWindow()
    try:
        qapp.processEvents()
        driver = window._aura_phase_driver
        assert driver.parent() is window
        assert window._chat._aura_phase_driver is driver
        assert window._playground_aura._phase_driver is driver

        window._chat.begin_assistant()
        assert window._chat._current_aura is not None
        assert window._chat._current_aura._phase_driver is driver
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
