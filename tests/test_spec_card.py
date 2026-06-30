"""Tests for SpecCard — Plan Ready cockpit card."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from aura.conversation.dispatch import WorkerOutcomeStatus
from aura.gui.cards.plan_writer_card import PlanWriterCard
from aura.gui.cards.spec_card import SpecCard


@pytest.fixture(scope="session")
def qapp():
    """Ensure a QApplication exists for widget tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def spec_card(qapp):
    """Create a fresh SpecCard for each test."""
    return SpecCard(
        "test_tc_id",
        "Fix the login bug",
        ["src/auth.py", "tests/test_auth.py"],
        "## Objective\nFix login.\n\n## Plan\nRefactor the auth module.",
        "All tests pass",
        summary="Refactor auth module to fix login flow",
    )


class TestCurrentSpec:
    """current_spec() returns 5 values."""

    def test_returns_five_values(self, spec_card):
        result = spec_card.current_spec()
        assert len(result) == 5
        goal, files, spec, acceptance, summary = result
        assert goal == "Fix the login bug"
        assert files == ["src/auth.py", "tests/test_auth.py"]
        assert "Fix login" in spec
        assert acceptance == "All tests pass"
        assert summary == "Refactor auth module to fix login flow"

    def test_summary_preserved_after_update(self, spec_card):
        spec_card.update_spec(
            "New goal", ["new.py"], "New spec", "New acc", summary="New summary"
        )
        _, _, _, _, summary = spec_card.current_spec()
        assert summary == "New summary"

    def test_files_preserved_after_update(self, spec_card):
        spec_card.update_spec(
            "New goal", ["a.py", "b.py"], "New spec", "New acc", summary="s"
        )
        _, files, _, _, _ = spec_card.current_spec()
        assert files == ["a.py", "b.py"]


class TestStrategyText:
    """STRATEGY section should not duplicate full spec."""

    def test_strategy_prefers_summary(self, spec_card):
        # summary is set, so strategy should use it
        text = spec_card._compute_strategy_text()
        assert text == "Refactor auth module to fix login flow"

    def test_strategy_truncates_spec_when_no_summary(self, qapp):
        card = SpecCard(
            "tid", "goal", ["f.py"],
            "## Objective\nDo something.\n\n## Details\n" + "x" * 500,
            "acc", summary=""
        )
        text = card._compute_strategy_text()
        # Should NOT be the full spec
        assert len(text) <= 303  # 300 + "…"
        assert "Objective" not in text or text.startswith("Do something")

    def test_strategy_never_returns_full_long_spec(self, qapp):
        long_spec = "## Heading\n" + ("A" * 1000)
        card = SpecCard("tid", "goal", ["f.py"], long_spec, "acc", summary="")
        text = card._compute_strategy_text()
        assert len(text) < len(long_spec)
        assert len(text) <= 303


class TestChips:
    """Computed chips should reflect spec characteristics."""

    def test_mode_chip_fast_plan(self, qapp):
        card = SpecCard("tid", "goal", ["f.py"], "short spec", "acc", summary="")
        card._compute_chips()
        assert "Fast Plan" in card._mode_chip.text()

    def test_mode_chip_careful_plan(self, qapp):
        card = SpecCard("tid", "goal", ["a.py", "b.py", "c.py"], "x" * 900, "acc", summary="")
        card._compute_chips()
        assert "Careful Plan" in card._mode_chip.text()

    def test_risk_chip_high_risk_auth(self, qapp):
        card = SpecCard("tid", "Fix auth token", ["auth.py"], "Use subprocess for auth", "acc", summary="")
        card._compute_chips()
        assert "High Risk" in card._risk_chip.text()

    def test_risk_chip_low_risk(self, qapp):
        card = SpecCard("tid", "Add docstring", ["README.md"], "Add docstring", "acc", summary="")
        card._compute_chips()
        assert "Low Risk" in card._risk_chip.text()

    def test_scope_chip_shows_file_count(self, qapp):
        card = SpecCard("tid", "goal", ["a.py", "b.py", "c.py"], "spec", "acc", summary="")
        card._compute_chips()
        assert "3 files" in card._scope_chip.text()

    def test_scope_chip_no_files(self, qapp):
        card = SpecCard("tid", "goal", [], "spec", "acc", summary="")
        card._compute_chips()
        assert "No files" in card._scope_chip.text()


class TestFullSpecCollapsed:
    """FULL WORKER SPEC should always be collapsed by default."""

    def test_full_spec_section_exists(self, spec_card):
        assert spec_card._raw_spec_section is not None

    def test_full_spec_starts_collapsed(self, spec_card):
        assert spec_card._raw_spec_section._open is False
        assert not spec_card._spec_body_label.isVisible()

    def test_full_spec_renders_regex_backslashes_in_code_fences(self, qapp):
        spec = (
            "Update regex handling.\n\n"
            "```python\n"
            "import re\n"
            "pattern = re.compile(r\"\\s+\")\n"
            "```\n"
        )

        card = SpecCard("tid", "goal", ["f.py"], spec, "acc", summary="")

        assert card.current_spec()[2] == spec


class TestCampaignRendering:
    def test_flat_dispatch_keeps_edit_button(self, qapp):
        card = SpecCard("tid", "goal", ["f.py"], "spec", "acc", summary="")

        button_texts = [button.text() for button in card.findChildren(QPushButton)]

        assert "Dispatch" in button_texts
        assert "Edit Spec" in button_texts
        assert "Cancel" in button_texts
        assert card._edit_btn is not None

    def test_campaign_dispatch_shows_steps_and_no_edit_button(self, qapp):
        card = SpecCard(
            "tid",
            "goal",
            ["shared.py"],
            "flat spec ignored for campaign rendering",
            "flat acceptance ignored for campaign rendering",
            summary="campaign",
            steps=[
                {"id": "one", "title": "Prepare helper", "files": ["a.py"]},
                {"id": "two", "title": "Wire call site", "files": ["b.py", "c.py"]},
            ],
        )

        button_texts = [button.text() for button in card.findChildren(QPushButton)]
        header_texts = [label.text() for label in card.findChildren(QLabel)]
        campaign_text = card._campaign_label.toPlainText()

        assert "Dispatch" in button_texts
        assert "Cancel" in button_texts
        assert "Edit Spec" not in button_texts
        assert card._edit_btn is None
        assert "CAMPAIGN" in header_texts
        assert "STRATEGY" not in header_texts
        assert "VALIDATION" not in header_texts
        assert campaign_text.index("Prepare helper") < campaign_text.index("Wire call site")
        assert "a.py" in campaign_text
        assert "b.py" in campaign_text
        assert "c.py" in campaign_text


class TestToolCallId:
    def test_tool_call_id(self, spec_card):
        assert spec_card.tool_call_id() == "test_tc_id"


class TestDispatchExpired:
    """mark_dispatch_expired() behaviour."""

    def test_mark_dispatch_expired(self, spec_card):
        spec_card.mark_dispatch_expired()
        assert not spec_card._buttons_row.isVisible()
        # Use isHidden() — isVisible() requires an ancestor window to be shown.
        assert not spec_card._status_label.isHidden()
        assert spec_card._status_label.text() == "Plan expired — click Dispatch again or Cancel"

    def test_mark_stale_after_on_dispatch(self, spec_card):
        """Simulate clicking Dispatch then calling mark_stale."""
        spec_card._on_dispatch()
        spec_card.mark_stale()
        assert not spec_card._buttons_row.isVisible()
        assert not spec_card._status_label.isHidden()
        assert spec_card._status_label.text() == "Stale plan — not pending"

    def test_worker_finished_user_facing_no_worker_word(self, spec_card):
        """worker_finished() status must not contain the word 'Worker'."""
        spec_card.worker_finished(True, "All good.")
        assert "Worker" not in spec_card._status_label.text()

        spec_card.worker_finished(False, "Something broke.")
        assert "Worker" not in spec_card._status_label.text()

    def test_worker_finished_uses_status_specific_label(self, spec_card):
        spec_card.worker_finished(
            False,
            "Validation failed.",
            status=WorkerOutcomeStatus.validation_failed.value,
        )
        assert spec_card._status_label.text() == "Validation failed"

        spec_card.worker_finished(
            False,
            "Craft blocked.",
            status=WorkerOutcomeStatus.craft_blocked.value,
        )
        assert spec_card._status_label.text() == "Craft blocked"

        spec_card.worker_finished(
            False,
            "Approval rejected.",
            status=WorkerOutcomeStatus.approval_rejected.value,
        )
        assert spec_card._status_label.text() == "Approval rejected"


class TestPlanWriterCard:
    def test_dispatch_not_started_is_not_shown_as_plan_ready(self, qapp):
        card = PlanWriterCard()
        card.set_result(
            True,
            '{"ok": false, "recoverable": true, '
            '"extras": {"dispatch_not_started": true, "pure_research": true}}',
        )

        assert card._state == PlanWriterCard.STATE_NOT_STARTED
        assert card._incomplete_text == "⚡ Plan blocked — research only"

    def test_dispatch_timeout_is_not_shown_as_plan_ready(self, qapp):
        card = PlanWriterCard()
        card.set_result(
            True,
            '{"ok": false, "recoverable": true, '
            '"extras": {"dispatch_not_started": true, "dispatch_approval_timeout": true}}',
        )

        assert card._state == PlanWriterCard.STATE_NOT_STARTED
        assert card._incomplete_text == "⚡ Plan expired"
