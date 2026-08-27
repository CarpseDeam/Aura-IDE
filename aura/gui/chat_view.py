"""Chat transcript: scrollable column of message cards."""
from __future__ import annotations

import json

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.conversation.chat_transcript import (
    assistant_item,
    clone_chat_items,
    error_item,
    plan_review_item,
    user_item,
)
from aura.gui.cards._helpers import _fade_in_widget
from aura.gui.cards.assistant_card import AssistantCard
from aura.gui.cards.code_writer_card import CodeWriterCard
from aura.gui.cards.diff_card import DiffCard
from aura.gui.cards.error_card import ErrorCard
from aura.gui.cards.plan_review_card import PlanReviewCard
from aura.gui.cards.user_card import UserCard
from aura.gui.controllers import ToolStreamController
from aura.gui.theme import (
    FG,
    FG_ITALIC,
)
from aura.gui.widgets.aura_glow import AuraPhaseDriver, AuraWidget


class ChatView(QScrollArea):
    """Vertical, scrollable column of cards."""

    retry_requested = Signal()
    mermaid_detected = Signal(str)  # emits the raw mermaid code
    droneRunFocusRequested = Signal(str)
    _BOTTOM_THRESHOLD_PX = 64
    _BOTTOM_SAFE_MARGIN_PX = 44

    def __init__(self, aura_phase_driver: AuraPhaseDriver) -> None:
        super().__init__()
        self._aura_phase_driver = aura_phase_driver
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        container.setMinimumWidth(0)
        container.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(20, 20, 20, self._BOTTOM_SAFE_MARGIN_PX)
        self._layout.setSpacing(32)
        self._layout.addStretch(1)
        self.setWidget(container)

        self._current_assistant: AssistantCard | None = None
        self._current_aura: AuraWidget | None = None
        self._latest_user_card: UserCard | None = None
        # Map tool_call_id -> the assistant card that owns it (for routing diff-after).
        self._tool_owner: dict[str, AssistantCard] = {}
        # Map tool_call_id -> ToolStreamController.
        self._controllers: dict[str, ToolStreamController] = {}
        # Per assistant turn: route repeated write/edit calls for the same path
        # into one visible CodeWriterCard.
        self._code_cards_by_path: dict[str, CodeWriterCard] = {}
        self._tool_to_code_card: dict[str, CodeWriterCard] = {}
        self._code_card_paths_by_tool: dict[str, str] = {}
        self._pending_code_content: dict[str, str] = {}
        self._pending_code_results: dict[str, bool] = {}
        self._empty_hint: QLabel | None = None
        self._scroll_anim: QPropertyAnimation | None = None
        self._programmatic_scroll_depth = 0
        self._chat_items: list[dict] = []
        self._record_transcript: bool = True
        self._current_assistant_transcript_parts: list[str] = []
        self._current_assistant_transcript_recorded: bool = False
        self._compact_tools: bool = False
        self._compact_tool_names: dict[str, str] = {}
        self._is_bulk_updating: bool = False
        self._show_empty_hint()

        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(40)
        self._scroll_timer.timeout.connect(self._scroll_to_bottom)

        # Follow new content while the user is reading the bottom of the thread.
        # If they scroll upward, leave the viewport alone until they return.
        self._auto_follow_bottom = True
        self._last_scroll_max = 0
        self.verticalScrollBar().rangeChanged.connect(self._on_scroll_range_changed)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)
        self.viewport().installEventFilter(self)
        self.verticalScrollBar().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if watched in (self.viewport(), self.verticalScrollBar()):
            if event.type() in (
                QEvent.Type.Wheel,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.KeyPress,
            ):
                self._stop_scroll_animation()
        return super().eventFilter(watched, event)

    def _on_scroll_range_changed(self, min_val: int, max_val: int) -> None:
        """If we were at the bottom before the range increased, stay at the bottom."""
        if max_val > self._last_scroll_max and self._auto_follow_bottom:
            self._stop_scroll_animation()
            self._set_scrollbar_to_bottom()
        self._last_scroll_max = max_val

    def _on_scroll_value_changed(self, value: int) -> None:
        if self._programmatic_scroll_depth > 0:
            return
        bar = self.verticalScrollBar()
        self._auto_follow_bottom = (
            bar.maximum() - value <= self._BOTTOM_THRESHOLD_PX
        )

    # ---- container management --------------------------------------------

    def begin_bulk_update(self) -> None:
        """Suspend animations and scrolling for bulk card insertion."""
        self._is_bulk_updating = True

    def end_bulk_update(self) -> None:
        """Resume animations and scrolling, and sync the view."""
        self._is_bulk_updating = False
        self.scroll_to_bottom(force=True)

    def _add_card(self, w: QWidget) -> None:
        if self._empty_hint is not None:
            self._empty_hint.deleteLater()
            self._empty_hint = None
        # Ensure parentage to prevent "window flashes"
        if w.parent() is None:
            w.setParent(self)
        # Insert before the trailing stretch.
        w.setMinimumWidth(0)
        w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._layout.insertWidget(self._layout.count() - 1, w)
        if not self._is_bulk_updating:
            _fade_in_widget(w)
            self._scroll_to_bottom()

    def _is_at_bottom(self, threshold: int = 30) -> bool:
        bar = self.verticalScrollBar()
        return bar.maximum() - bar.value() <= threshold

    def _scroll_to_bottom(self, force: bool = False) -> None:
        self._scroll_timer.stop()
        if self._is_bulk_updating and not force:
            return
        if not force and not self._auto_follow_bottom:
            return
        self._auto_follow_bottom = True
        bar = self.verticalScrollBar()
        if bar.value() == bar.maximum():
            return
        self._stop_scroll_animation()
        self._scroll_anim = QPropertyAnimation(bar, b"value")
        self._scroll_anim.setDuration(150)
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(bar.maximum())
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.finished.connect(self._end_programmatic_scroll)
        self._programmatic_scroll_depth += 1
        self._scroll_anim.start()

    def _request_scroll_to_bottom(self, force: bool = False) -> None:
        if force:
            self._scroll_timer.stop()
            self._scroll_to_bottom(force=True)
        elif not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _stop_scroll_animation(self) -> None:
        if self._scroll_anim is not None:
            self._scroll_anim.stop()
            self._scroll_anim.deleteLater()
            self._scroll_anim = None
            self._programmatic_scroll_depth = 0

    def _end_programmatic_scroll(self) -> None:
        self._programmatic_scroll_depth = max(0, self._programmatic_scroll_depth - 1)
        if self._scroll_anim is not None:
            self._scroll_anim.deleteLater()
            self._scroll_anim = None

    def _set_scrollbar_to_bottom(self, force: bool = False) -> None:
        if not force and not self._auto_follow_bottom:
            return
        bar = self.verticalScrollBar()
        self._programmatic_scroll_depth += 1
        bar.setValue(bar.maximum())
        self._programmatic_scroll_depth = max(0, self._programmatic_scroll_depth - 1)

    def scroll_to_bottom(self, force: bool = False) -> None:
        """Move to the newest content, with delayed passes for late layout changes."""
        self._scroll_to_bottom(force=force)
        if force:
            self._auto_follow_bottom = True
            for delay in (0, 50, 150):
                QTimer.singleShot(delay, lambda: self._set_scrollbar_to_bottom(force=True))

    def set_compact_tools(self, enabled: bool) -> None:
        self._compact_tools = enabled

    def add_drone_run_badge(self, run_id: str, drone_name: str) -> None:
        ac = self._current_assistant
        if ac is None:
            return
        ac.add_drone_run_badge(
            run_id,
            drone_name,
            self.droneRunFocusRequested.emit,
        )
        self._scroll_to_bottom()

    def _scroll_after_bottom_layout_change(self) -> None:
        """Force delayed bottom sync after UI below the chat changes height."""
        self.scroll_to_bottom(force=True)

    def _show_empty_hint(self) -> None:
        hint = QLabel(
            "Start by describing the bug, dragging in code, or pasting a screenshot."
        )
        hint.setStyleSheet(f"color: {FG_ITALIC}; font-style: italic;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.insertWidget(0, hint)
        self._empty_hint = hint

    # ---- mutation API -----------------------------------------------------

    def reset(self) -> None:
        self._scroll_timer.stop()
        # Strip everything except the trailing stretch.
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._current_assistant = None
        self._current_aura = None
        self._latest_user_card = None
        self._tool_owner.clear()
        self._chat_items.clear()
        self._current_assistant_transcript_parts.clear()
        self._current_assistant_transcript_recorded = False
        self._controllers.clear()
        self._clear_code_card_routes()
        self._compact_tool_names.clear()
        self._auto_follow_bottom = True
        self._last_scroll_max = 0
        self._empty_hint = None
        self._show_empty_hint()

    @property
    def chat_items(self) -> list[dict]:
        return clone_chat_items(self._chat_items)

    def begin_transcript_replay(self) -> None:
        self._record_transcript = False

    def end_transcript_replay(self, items: list[dict]) -> None:
        self._record_transcript = True
        self._chat_items = clone_chat_items(items)
        self._current_assistant_transcript_parts.clear()
        self._current_assistant_transcript_recorded = False

    def replay_messages(self, messages: list[dict]) -> None:
        """Legacy text-only replay helper.

        Normal conversation load must render durable ``chat_items`` instead.
        The caller is responsible for calling reset() first.
        """
        from aura.conversation.chat_transcript import legacy_chat_items_from_messages

        items = legacy_chat_items_from_messages(messages)
        self.begin_transcript_replay()
        for item in items:
            if item.get("kind") == "user":
                self.add_user(str(item.get("text", "")))
            elif item.get("kind") == "assistant":
                self.begin_assistant()
                self.append_content(str(item.get("text", "")))
                self.assistant_done()
        self.end_transcript_replay(items)

    def _add_tool_result_card(self, tool_call_id: str, content: str) -> None:
        """Add a compact card showing a tool result during history replay."""
        from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

        card = QFrame(self)
        card.setObjectName("toolResultCard")
        card.setStyleSheet(
            "QFrame#toolResultCard {"
            "  background: rgba(82, 148, 226, 0.06);"
            "  border: 1px solid rgba(82, 148, 226, 0.2);"
            "  border-radius: 6px;"
            "}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
        heading = QLabel("\U0001f50c Tool result")
        heading.setStyleSheet("color: #5294E2; font-size: 11px; font-weight: 600;")
        layout.addWidget(heading)
        body = QLabel(content[:500])
        body.setWordWrap(True)
        body.setStyleSheet("color: #ccc; font-size: 11px;")
        body.setTextInteractionFlags(
            body.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(body)
        self._add_card(card)

    def add_user(self, text: str, image_b64s: list[str] | None = None) -> None:
        # Slight right inset on user cards so the conversation rhythm is visible at a glance —
        # not a chat-bubble alignment, just enough to feel like input vs. output.
        wrapper = QWidget(self)
        wrapper.setStyleSheet("background: transparent;")
        h = QHBoxLayout(wrapper)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        if self._latest_user_card is not None:
            self._latest_user_card.set_rerun_visible(False)

        card = UserCard(text, image_b64s, parent=wrapper)
        h.addWidget(card, 1)
        h.addSpacing(40)
        self._add_card(wrapper)
        self._latest_user_card = card
        card.set_rerun_visible(True)
        card.rerun_requested.connect(self.retry_requested.emit)
        self._current_assistant = None  # next assistant turn opens a new card
        if self._record_transcript:
            self._chat_items.append(user_item(text, image_b64s))

    def begin_assistant(self) -> AssistantCard:
        self._clear_code_card_routes()
        card = AssistantCard(compact_tools=self._compact_tools, parent=self)
        card._chat_view = self
        self._current_assistant = card
        wrapper = AuraWidget(
            card,
            phase_driver=self._aura_phase_driver,
            glow_spread=16,
            parent=self,
        )
        self._current_aura = wrapper
        self._add_card(wrapper)
        wrapper.start_aura()
        self._current_assistant_transcript_parts = []
        self._current_assistant_transcript_recorded = False
        return card

    def current_assistant(self) -> AssistantCard:
        if self._current_assistant is None:
            return self.begin_assistant()
        return self._current_assistant

    def _clear_code_card_routes(self) -> None:
        self._code_cards_by_path.clear()
        self._tool_to_code_card.clear()
        self._code_card_paths_by_tool.clear()
        self._pending_code_content.clear()
        self._pending_code_results.clear()

    def _code_card_key(self, path: str) -> str:
        return path.strip()

    def _ensure_tool_cluster_visible(self, ac: AssistantCard) -> None:
        if not ac._tool_cluster.isVisible():
            ac._tool_cluster.setVisible(True)

    def _resolve_code_card(
        self, tool_call_id: str, name: str, path: str, ac: AssistantCard
    ) -> None:
        key = self._code_card_key(path)
        if not key:
            return

        card = self._tool_to_code_card.get(tool_call_id)
        if card is None:
            card = self._code_cards_by_path.get(key)
            if card is None:
                card = CodeWriterCard(name, parent=self)
                self._ensure_tool_cluster_visible(ac)
                ac._tool_cluster_layout.addWidget(card)
                self._code_cards_by_path[key] = card
            card.begin_update(name)
            self._tool_to_code_card[tool_call_id] = card
            self._code_card_paths_by_tool[tool_call_id] = key
        else:
            old_key = self._code_card_paths_by_tool.get(tool_call_id)
            if old_key and old_key != key and self._code_cards_by_path.get(old_key) is card:
                self._code_cards_by_path.pop(old_key, None)
            self._code_cards_by_path[key] = card
            self._code_card_paths_by_tool[tool_call_id] = key

        card.set_target_path(path)
        pending_content = self._pending_code_content.pop(tool_call_id, None)
        if pending_content is not None:
            card.update_content(pending_content)
        pending_result = self._pending_code_results.pop(tool_call_id, None)
        if pending_result is not None:
            card.set_result(pending_result)
        self._scroll_to_bottom()

    def _update_code_content(self, tool_call_id: str, content: str) -> None:
        card = self._tool_to_code_card.get(tool_call_id)
        if card is None:
            self._pending_code_content[tool_call_id] = content
            return
        card.update_content(content)

    def _set_code_result(self, tool_call_id: str, ok: bool) -> None:
        card = self._tool_to_code_card.get(tool_call_id)
        if card is None:
            self._pending_code_results[tool_call_id] = ok
            return
        card.set_result(ok)

    def _remove_failed_write_cards_from_assistant(self, ac: AssistantCard) -> None:
        """Remove failed CodeWriterCards from an assistant card's tool cluster.

        Failed write cards belong in the Execution Log/playground, not the main
        chat.  This is called:
        - after an execution completes,
        - at the end of each assistant turn (assistant_done).

        Execution write tool calls are routed to the playground, but during
        conversation replay or a live execution a failed write card can
        appear in the main chat — we remove it here so the main chat ends
        with the assistant summary, not a scary "Failed ✗" card.
        """
        tool_cluster = ac._tool_cluster if hasattr(ac, '_tool_cluster') else None
        if tool_cluster is None:
            return
        layout = tool_cluster.layout()
        if layout is None:
            return
        for i in range(layout.count() - 1, -1, -1):
            item = layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            if not isinstance(widget, CodeWriterCard):
                continue
            try:
                is_failed = widget._state == CodeWriterCard.STATE_FAILED
            except AttributeError:
                continue
            if is_failed:
                layout.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()

    def show_code_diff(
        self,
        tool_call_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
    ) -> None:
        card = self._tool_to_code_card.get(tool_call_id)
        if card is None:
            return
        card.set_target_path(rel_path)
        if decision in ("approve", "approve_all"):
            card.show_content_transition(old, new)
        else:
            card.update_content(old)
        self._scroll_to_bottom()

    def append_reasoning(self, text: str) -> None:
        self.current_assistant().append_reasoning(text)
        self._request_scroll_to_bottom()

    def append_content(self, text: str) -> None:
        ac = self.current_assistant()
        # The first content delta means reasoning is done.
        if not ac._content_label.isVisible():
            ac.reasoning_done()
        ac.append_content(text)
        if self._record_transcript:
            self._current_assistant_transcript_parts.append(text)
        self._request_scroll_to_bottom()

    def add_tool_call(self, tool_call_id: str, name: str) -> None:
        ac = self.current_assistant()
        ac.notify_compact_tool_start(name)
        self._compact_tool_names[tool_call_id] = name

        self._scroll_to_bottom()

    def append_tool_args(self, tool_call_id: str, fragment: str) -> None:
        if tool_call_id in self._compact_tool_names:
            return
        controller = self._controllers.get(tool_call_id)
        if controller:
            controller.append_fragment(fragment)
            self._request_scroll_to_bottom()

    def set_tool_result(self, tool_call_id: str, ok: bool, result_text: str) -> None:
        if tool_call_id in self._compact_tool_names:
            name = self._compact_tool_names.pop(tool_call_id, "tool")
            ac = self.current_assistant()
            ac.notify_compact_tool_done(name)
            return

        controller = self._controllers.pop(tool_call_id, None)
        if controller:
            controller.finalize(ok, result_text)
            self._scroll_to_bottom()

    def _tool_goal_from_args(self, args_text: str) -> str:
        try:
            data = json.loads(args_text)
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("goal") or data.get("objective") or "").strip()

    def add_diff_card(
        self,
        owner_tool_call_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        # Attach diff card to the assistant that owned the tool call.
        ac = self._tool_owner.get(owner_tool_call_id) or self.current_assistant()
        card = DiffCard(rel_path, old, new, decision, is_new_file, parent=self)
        # Append as a footer under the assistant card.
        ac.add_footer_widget(card)
        self._scroll_to_bottom()

    def add_plan_review_card(
        self,
        review_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str = "",
    ) -> PlanReviewCard:
        """Attach a Plan Review card to the current assistant turn.

        Used both for the live interactive card (the caller wires its
        signals to the proxy) and, with an empty ``review_id`` immediately
        followed by ``card.show_resolved(...)``, for non-interactive replay.
        """
        ac = self.current_assistant()
        card = PlanReviewCard(review_id, goal, files, spec, acceptance, summary, parent=self)
        ac.add_footer_widget(card)
        self._scroll_to_bottom()
        return card

    def record_plan_review(
        self,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str,
        *,
        approved: bool,
        user_edited: bool = False,
    ) -> None:
        """Record a resolved Plan Review decision into the durable transcript."""
        if self._record_transcript:
            self._chat_items.append(
                plan_review_item(
                    goal=goal,
                    files=files,
                    spec=spec,
                    acceptance=acceptance,
                    summary=summary,
                    approved=approved,
                    user_edited=user_edited,
                )
            )

    def add_info(self, title: str, message: str) -> None:
        """Add a neutral informational card to the chat."""
        from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

        card = QFrame(self)
        card.setObjectName("infoCard")
        card.setStyleSheet(
            "QFrame#infoCard {"
            "  background: rgba(157, 124, 216, 0.08);"
            "  border: 1px solid rgba(157, 124, 216, 0.35);"
            "  border-radius: 10px;"
            "}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        head = QLabel(title, card)
        head.setStyleSheet("color: #9d7cd8; font-weight: 600;")
        layout.addWidget(head)
        body = QLabel(message, card)
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            body.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.setStyleSheet(f"color: {FG};")
        layout.addWidget(body)
        self._add_card(card)

    def add_error(
        self,
        title: str,
        message: str,
        show_retry: bool = False,
        persist: bool = True,
    ) -> None:
        """Show a failure card and, by default, record it in the transcript.

        Errors are durable conversation state: an API failure the user saw must
        still be there after a restart.  ``persist=False`` is for notices about
        the session itself (a failed save), which must not be written back.
        """
        card = ErrorCard(title, message, show_retry=show_retry, parent=self)
        if show_retry:
            card.retry_clicked.connect(self.retry_requested.emit)
        self._add_card(card)
        if self._record_transcript and persist:
            self._chat_items.append(error_item(title, message, show_retry))

    def stop_current_aura(self) -> None:
        if self._current_aura is not None:
            self._current_aura.stop_aura()

    def assistant_done(self) -> None:
        ac = self._current_assistant
        if ac is None:
            return
        self._record_current_assistant_transcript()
        ac.finalize_content()
        # Clean up any failed CodeWriterCards — write failures belong in
        # the Execution Log, not the main chat.  Call before stopping the aura
        # so the user sees a clean assistant summary as the final item.
        self._remove_failed_write_cards_from_assistant(ac)
        # Stop the breathing glow — content is complete, no need to pulse anymore.
        if self._current_aura is not None:
            self._current_aura.stop_aura()
        # Tighten final scroll with delayed reflow for layout settling,
        # but only if the user hasn't scrolled up.
        if self._auto_follow_bottom:
            self.scroll_to_bottom(force=True)
        else:
            self._scroll_to_bottom()

    def finalize_markdown_only(self) -> None:
        """Finalize Markdown rendering without stopping the breathing aura.

        Use this when the stream has ended but the turn is still running (a
        tool-call round, or waiting for dispatch resolution) so the aura should
        keep pulsing.  The transcript is deliberately *not* recorded here: the
        assistant turn is incomplete, and recording it now would both freeze a
        partial answer and lock out the real one, which appends to this same
        card in a later round.
        """
        ac = self._current_assistant
        if ac is not None:
            ac.finalize_content()
            self._scroll_to_bottom()

    def _record_current_assistant_transcript(self) -> None:
        if not self._record_transcript:
            return
        if self._current_assistant_transcript_recorded:
            return
        text = "".join(self._current_assistant_transcript_parts).strip()
        if not text:
            return
        self._chat_items.append(assistant_item(text))
        self._current_assistant_transcript_recorded = True
