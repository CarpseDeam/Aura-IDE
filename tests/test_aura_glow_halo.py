"""Lifecycle and rendering regression checks for AuraWidget's fixed-geometry
halo: start/stop easing, single-owned color/envelope animations, and an
offscreen render proving the halo wraps all four edges (not vertical bands).
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QAbstractAnimation, QPoint  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from aura.gui.widgets.aura_glow import AuraWidget  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


def _make_widget(qapp, w=500, h=150, inner_w=400, inner_h=60, spread=16) -> AuraWidget:
    inner = QLabel("card")
    inner.setFixedSize(inner_w, inner_h)
    widget = AuraWidget(inner, glow_color="#00e5ff", glow_spread=spread)
    widget.resize(w, h)
    return widget


def _render_alpha(widget: AuraWidget, x: int, y: int) -> int:
    img = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    widget.render(img)
    return img.pixelColor(x, y).alpha()


def test_start_aura_runs_breath_and_fades_envelope_in(qapp) -> None:
    widget = _make_widget(qapp)
    widget.start_aura()

    assert widget._breath_anim.state() == QAbstractAnimation.State.Running
    assert widget._envelope == 0.0  # not yet advanced

    widget._envelope_anim.setCurrentTime(widget._FADE_DURATION_MS)
    assert widget._envelope == pytest.approx(1.0)


def test_stop_aura_eases_out_then_goes_fully_idle(qapp) -> None:
    widget = _make_widget(qapp)
    widget.start_aura()
    widget._envelope_anim.setCurrentTime(widget._FADE_DURATION_MS)
    assert widget._envelope == pytest.approx(1.0)

    widget.stop_aura()
    # Mid-fade: envelope eased down but breathing hasn't been torn off yet.
    widget._envelope_anim.setCurrentTime(widget._FADE_DURATION_MS // 2)
    assert 0.0 < widget._envelope < 1.0
    assert widget._breath_anim.state() == QAbstractAnimation.State.Running

    # Fade completes -> breathing stops and the widget goes idle/transparent.
    widget._envelope_anim.setCurrentTime(widget._FADE_DURATION_MS)
    assert widget._envelope == pytest.approx(0.0)
    assert widget._breath_anim.state() == QAbstractAnimation.State.Stopped

    assert _render_alpha(widget, widget.width() // 2, 2) == 0


def test_repeated_state_transitions_keep_single_color_animation(qapp) -> None:
    widget = _make_widget(qapp)
    widget.set_glow_state("thinking")
    first_anim = widget._color_anim
    assert first_anim is not None
    assert first_anim.state() == QAbstractAnimation.State.Running

    widget.set_glow_state("coding")
    second_anim = widget._color_anim
    assert second_anim is not first_anim
    assert first_anim.state() == QAbstractAnimation.State.Stopped
    assert second_anim.state() == QAbstractAnimation.State.Running

    widget.set_glow_state("thinking")
    third_anim = widget._color_anim
    assert third_anim is not second_anim
    assert second_anim.state() == QAbstractAnimation.State.Stopped
    assert third_anim.state() == QAbstractAnimation.State.Running


def test_repeated_start_stop_does_not_stack_envelope_animations(qapp) -> None:
    widget = _make_widget(qapp)
    widget.start_aura()
    first_envelope_anim = widget._envelope_anim
    widget.stop_aura()
    second_envelope_anim = widget._envelope_anim

    assert second_envelope_anim is not first_envelope_anim
    assert first_envelope_anim.state() == QAbstractAnimation.State.Stopped


def test_idle_widget_paints_nothing(qapp) -> None:
    widget = _make_widget(qapp)
    # Never started - should stay fully transparent.
    assert _render_alpha(widget, widget.width() // 2, 2) == 0
    assert _render_alpha(widget, 2, widget.height() // 2) == 0


def test_active_breath_never_fully_disappears_at_pulse_endpoints(qapp) -> None:
    widget = _make_widget(qapp)
    widget.start_aura()
    widget._envelope_anim.setCurrentTime(widget._FADE_DURATION_MS)

    # value=0.0 and value=1.0 are the sine's zero-crossings (pulse endpoints).
    widget._breath_anim.setCurrentTime(0)
    assert widget._breath == pytest.approx(widget._BREATH_FLOOR)
    assert widget._breath > 0.0

    widget._breath_anim.setCurrentTime(widget._BREATH_DURATION_MS)
    assert widget._breath == pytest.approx(widget._BREATH_FLOOR, abs=1e-2)


def test_halo_alpha_capped_below_legacy_ceiling(qapp) -> None:
    widget = _make_widget(qapp)
    widget.start_aura()
    widget._envelope_anim.setCurrentTime(widget._FADE_DURATION_MS)
    widget._breath_anim.setCurrentTime(widget._BREATH_DURATION_MS // 2)  # peak

    img = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    widget.render(img)

    # Only sample the halo's own ring region - the interior is the rendered
    # inner card (a QLabel here), whose own opaque pixels are unrelated to
    # the glow and would otherwise pollute this check.
    region = widget._cached_ring_region
    assert region is not None
    max_alpha = 0
    for x in range(0, widget.width(), 2):
        for y in range(0, widget.height(), 2):
            if region.contains(QPoint(x, y)):
                max_alpha = max(max_alpha, img.pixelColor(x, y).alpha())

    assert 0 < max_alpha < 240
    assert max_alpha <= widget._MAX_ALPHA + 1


def test_halo_geometry_fixed_across_breath_cycle(qapp) -> None:
    """Only opacity should animate - the ring path/region must not change
    shape as the breath value moves."""
    widget = _make_widget(qapp)
    widget.start_aura()
    widget._envelope_anim.setCurrentTime(widget._FADE_DURATION_MS)

    widget._breath_anim.setCurrentTime(0)
    layers_a = [(p.boundingRect(), w) for p, w, _ in widget._cached_halo_layers]

    widget._breath_anim.setCurrentTime(widget._BREATH_DURATION_MS // 2)
    layers_b = [(p.boundingRect(), w) for p, w, _ in widget._cached_halo_layers]

    assert layers_a == layers_b


@pytest.mark.parametrize(
    "w, h, inner_w, inner_h",
    [
        (1400, 100, 1200, 40),  # wide pane
        (140, 900, 60, 700),  # tall pane
    ],
)
def test_halo_wraps_all_four_edges_not_just_vertical_bands(qapp, w, h, inner_w, inner_h) -> None:
    widget = _make_widget(qapp, w=w, h=h, inner_w=inner_w, inner_h=inner_h, spread=16)
    widget.start_aura()
    widget._envelope_anim.setCurrentTime(widget._FADE_DURATION_MS)
    widget._breath_anim.setCurrentTime(widget._BREATH_DURATION_MS // 2)

    img = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    widget.render(img)

    # Sample mid-band rather than the outermost pixels: the feather is
    # designed to taper all the way to transparent right at the true outer
    # boundary, so the meaningful check is that color reaches partway into
    # the margin on every side, not that the very last pixel is lit.
    W, H = widget.width(), widget.height()
    band = max(1, widget._glow_spread // 2)
    top_mid = img.pixelColor(W // 2, band).alpha()
    bottom_mid = img.pixelColor(W // 2, H - band).alpha()
    left_mid = img.pixelColor(band, H // 2).alpha()
    right_mid = img.pixelColor(W - band, H // 2).alpha()

    edges = {"top": top_mid, "bottom": bottom_mid, "left": left_mid, "right": right_mid}
    for name, alpha in edges.items():
        assert alpha > 0, f"expected halo on {name} edge, got alpha={alpha}"

    # Not brittle exact-pixel matching - just confirm no single edge is
    # wildly starved relative to the rest (the old radial-center approach
    # would concentrate intensity in two opposite bands on an extreme
    # aspect ratio, leaving the other pair near zero).
    values = list(edges.values())
    assert min(values) > 0
    assert max(values) / min(values) < 4
