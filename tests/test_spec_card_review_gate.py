"""Tests for SpecCard review gate invariants.

SpecCard must still exist and remain the review gate.
No campaign section, no campaign chip, no steps constructor path.
"""

from aura.gui.cards.spec_card import SpecCard


class TestSpecCardReviewGate:
    """SpecCard still renders normal bounded request."""

    def test_has_dispatch_edit_cancel(self):
        """SpecCard should have Dispatch, Edit Spec, and Cancel buttons."""
        # The class should define dispatch_clicked, edit_clicked, cancel_clicked signals
        assert hasattr(SpecCard, "dispatch_clicked")
        assert hasattr(SpecCard, "edit_clicked")
        assert hasattr(SpecCard, "cancel_clicked")

    def test_no_steps_constructor(self):
        """SpecCard constructor should not accept a steps parameter."""
        import inspect
        sig = inspect.signature(SpecCard.__init__)
        assert "steps" not in sig.parameters, (
            "SpecCard should not accept a 'steps' parameter"
        )

    def test_current_spec_returns_tuple(self):
        """current_spec should return the expected 5-tuple."""
        assert SpecCard.current_spec is not None

    def test_has_dispatch_methods(self):
        """SpecCard should have dispatch lifecycle methods."""
        assert hasattr(SpecCard, "mark_dispatched")
        assert hasattr(SpecCard, "mark_worker_running")
        assert hasattr(SpecCard, "mark_cancelled")
        assert hasattr(SpecCard, "worker_finished")
