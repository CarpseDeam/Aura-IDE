"""Chat transcript: scrollable column of message cards."""
from __future__ import annotations

import json

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards._helpers import _fade_in_widget
from aura.gui.cards.assistant_card import AssistantCard
from aura.gui.cards.code_writer_card import CodeWriterCard
from aura.gui.cards.diff_card import DiffCard
from aura.gui.cards.error_card import ErrorCard
from aura.gui.cards.mismatch_resolution_card import MismatchResolutionCard
from aura.gui.cards.plan_writer_card import PlanWriterCard
from aura.gui.cards.spec_card import SpecCard
from aura.gui.cards.terminal_card import TerminalCard
from aura.gui.cards.user_card import UserCard
from aura.gui.cards.worker_summary_card import WorkerSummaryCard
from aura.gui.controllers import ToolStreamController
from aura.gui.theme import (
    ACCENT,
    FG,
    FG_ITALIC,
)
from aura.gui.widgets.aura_glow import AuraWidget


class ChatView(QScrollArea):
    """Vertical, scrollable column of cards."""

    retry_requested = Signal()
    mermaid_detected = Signal(str)  # emits the raw mermaid code
    _CODE_TOOL_NAMES = {"write_file", "apply_edit_transaction", "edit_file", "edit_symbol", "edit_line_range", "patch_file"}
    # Internal execution tools that always use compact badge (never create cards)
    _INTERNAL_TOOLS = {"run_terminal_command", "run_diagnostic_command"}
    _BOTTOM_THRESHOLD_PX = 64
    _BOTTOM_SAFE_MARGIN_PX = 44

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(20, 20, 20, self._BOTTOM_SAFE_MARGIN_PX)
        self._layout.setSpacing(32)
        self._layout.addStretch(1)
        self.setWidget(container)

        self._current_assistant: AssistantCard | None = None
        self._current_aura: AuraWidget | None = None
        # Map tool_call_id -> the assistant card that owns it (for routing diff-after).
        self._tool_owner: dict[str, AssistantCard] = {}
        # Map dispatch tool_call_id -> SpecCard.
        self._spec_cards: dict[str, SpecCard] = {}
        # Map tool_call_id -> TerminalCard.
        self._terminal_cards: dict[str, TerminalCard] = {}
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
        self._plan_writer_cards: dict[str, PlanWriterCard] = {}
        self._worker_summary_cards: dict[str, WorkerSummaryCard] = {}
        self._worker_summary_disabled: bool = True
        self._mismatch_resolution_cards: dict[str, MismatchResolutionCard] = {}
        self._compact_tools: bool = False
        self._compact_tool_names: dict[str, str] = {}
        self._is_bulk_updating: bool = False
        self._show_empty_hint()

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
        self._layout.insertWidget(self._layout.count() - 1, w)
        if not self._is_bulk_updating:
            _fade_in_widget(w)
            self._scroll_to_bottom()

    def _is_at_bottom(self, threshold: int = 30) -> bool:
        bar = self.verticalScrollBar()
        return bar.maximum() - bar.value() <= threshold

    def _scroll_to_bottom(self, force: bool = False) -> None:
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
        # Strip everything except the trailing stretch.
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._current_assistant = None
        self._current_aura = None
        self._tool_owner.clear()
        self._spec_cards.clear()
        self._plan_writer_cards.clear()
        self._worker_summary_cards.clear()
        self._mismatch_resolution_cards.clear()
        self._terminal_cards.clear()
        self._controllers.clear()
        self._clear_code_card_routes()
        self._compact_tool_names.clear()
        self._auto_follow_bottom = True
        self._last_scroll_max = 0
        self._empty_hint = None
        self._show_empty_hint()

    def replay_messages(self, messages: list[dict]) -> None:
        """Replay saved messages into the chat view.

        The caller is responsible for calling reset() first.
        Messages are full API-format dicts: user, assistant, tool.
        """
        for msg in messages:
            role = msg.get("role", "")
            if role == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    self.add_user(content)
            elif role == "assistant":
                card = self.begin_assistant()
                reasoning = msg.get("reasoning_content")
                if reasoning:
                    card.append_reasoning(reasoning)
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    card.append_content(content)
                elif isinstance(content, list):
                    # Multi-modal content: extract text parts
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            card.append_content(part["text"])
                self.assistant_done()
            elif role == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and content:
                    self._add_tool_result_card(
                        msg.get("tool_call_id", ""), content[:2000]
                    )

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
        h.addWidget(UserCard(text, image_b64s, parent=wrapper), 1)
        h.addSpacing(40)
        self._add_card(wrapper)
        self._current_assistant = None  # next assistant turn opens a new card

    def begin_assistant(self) -> AssistantCard:
        self._clear_code_card_routes()
        card = AssistantCard(compact_tools=self._compact_tools, parent=self)
        card._chat_view = self
        self._current_assistant = card
        wrapper = AuraWidget(card, glow_color=ACCENT, glow_spread=16, parent=self)
        self._current_aura = wrapper
        self._add_card(wrapper)
        wrapper.start_aura()
        wrapper.set_glow_state("thinking")
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

    def _remove_plan_writer_card(self, tool_call_id: str) -> None:
        """Remove and delete the PlanWriterCard associated with *tool_call_id*."""
        card = self._plan_writer_cards.pop(tool_call_id, None)
        if card is None:
            return
        parent = card.parentWidget()
        if parent is not None:
            layout = parent.layout()
            if layout is not None:
                layout.removeWidget(card)
        # Reparent to None so findChildren() stops finding it immediately
        card.setParent(None)
        card.deleteLater()

    def prepare_spec_card(self, tool_call_id: str) -> None:
        """Remove transient plan-writing UI before showing an active spec card."""
        self._remove_plan_writer_card(tool_call_id)

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

    def _cleanup_failed_code_cards(self, tool_call_id: str) -> None:
        """Remove any failed CodeWriterCards from the correct assistant's tool cluster."""
        ac = self._tool_owner.get(tool_call_id)
        if ac is None:
            ac = self._current_assistant
        if ac is None:
            return
        self._remove_failed_write_cards_from_assistant(ac)

    def _remove_failed_write_cards_from_assistant(self, ac: AssistantCard) -> None:
        """Remove failed CodeWriterCards from an assistant card's tool cluster.

        Failed write cards belong in the Worker Log/playground, not the main
        chat.  This is called:
        - after dispatch_to_worker completes,
        - at the end of each assistant turn (assistant_done).

        Worker write tool calls are routed to the playground, but during
        conversation replay or single-mode execution a failed write card can
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
        if self._current_aura is not None:
            self._current_aura.set_glow_state("thinking")
        self._scroll_to_bottom()

    def append_content(self, text: str) -> None:
        ac = self.current_assistant()
        # The first content delta means reasoning is done.
        if not ac._content_label.isVisible():
            ac.reasoning_done()
        # On first content delta, ensure the glow is in "thinking" state
        # (important for planners that don't produce reasoning content).
        if not ac._content_label.isVisible() and self._current_aura is not None:
            self._current_aura.set_glow_state("thinking")
        ac.append_content(text)
        self._scroll_to_bottom()

    def add_tool_call(self, tool_call_id: str, name: str) -> None:
        if self._current_aura is not None:
            self._current_aura.set_glow_state("coding")

        # Heavy tools that should always show a card, even in compact mode.
        is_heavy = name in (
            "write_file",
            "apply_edit_transaction",
            "edit_file",
            "edit_symbol",
            "edit_line_range",
            "patch_file",
            "dispatch_to_worker",
            "run_research",
        )

        # Internal execution tools always use compact badge, never create cards
        if name in self._INTERNAL_TOOLS:
            ac = self.current_assistant()
            ac.notify_compact_tool_start(name)
            self._compact_tool_names[tool_call_id] = name
            self._scroll_to_bottom()
            return

        if self._compact_tools and not is_heavy:
            ac = self.current_assistant()
            ac.notify_compact_tool_start(name)
            self._compact_tool_names[tool_call_id] = name
            self._scroll_to_bottom()
            return

        # Reuse existing controller if this ID was already started (uncommon but possible in replay/retry)
        controller = self._controllers.get(tool_call_id)
        if controller is None:
            controller = ToolStreamController(name, parent=self)
            self._controllers[tool_call_id] = controller

        ac = self.current_assistant()
        if name in self._CODE_TOOL_NAMES:
            self._tool_owner[tool_call_id] = ac

            # Defer visible card creation until the target path is known. Once
            # resolved, multiple tool calls for the same path share one card.
            controller.path_resolved.connect(
                lambda path, tid=tool_call_id, tool=name, owner=ac: self._resolve_code_card(
                    tid, tool, path, owner
                )
            )
            controller.content_updated.connect(
                lambda content, tid=tool_call_id: self._update_code_content(
                    tid, content
                )
            )
            controller.state_changed.connect(
                lambda s, tid=tool_call_id: self._set_code_result(tid, s == "done")
            )

        elif name == "dispatch_to_worker" or name == "run_research":
            card = PlanWriterCard(parent=self)
            self._plan_writer_cards[tool_call_id] = card
            if not ac._tool_cluster.isVisible():
                ac._tool_cluster.setVisible(True)
            ac._tool_cluster_layout.addWidget(card)
            self._tool_owner[tool_call_id] = ac

            # Wire plan writer signals
            controller.goal_updated.connect(card.set_goal)
            controller.content_updated.connect(card.update_spec)
            controller.state_changed.connect(lambda s: card.set_result(s == "done"))
            controller.result_finalized_text.connect(
                lambda text, c=controller, card=card: card.set_result(c._state == "done", text)
            )

        else:
            card = ac.add_tool_card(tool_call_id, name)
            self._tool_owner[tool_call_id] = ac
            if card is not None:
                # Wire generic tool card signals
                controller.args_updated.connect(card.update_args)
                controller.result_finalized_text.connect(lambda text: card.set_result(controller._state == "done", text))

        self._scroll_to_bottom()

    def append_tool_args(self, tool_call_id: str, fragment: str) -> None:
        if tool_call_id in self._compact_tool_names:
            return
        controller = self._controllers.get(tool_call_id)
        if controller:
            controller.append_fragment(fragment)
            self._scroll_to_bottom()

    def set_tool_result(self, tool_call_id: str, ok: bool, result_text: str) -> None:
        if tool_call_id in self._compact_tool_names:
            name = self._compact_tool_names.pop(tool_call_id, "tool")
            ac = self.current_assistant()
            ac.notify_compact_tool_done(name)
            return

        controller = self._controllers.pop(tool_call_id, None)
        if controller:
            if controller.tool_name == "dispatch_to_worker":
                summary = ""
                needs_followup = False
                status = None
                dispatch_not_started = False
                approval_timeout = False
                cancelled = False
                recoverable = False
                try:
                    data = json.loads(result_text)
                    extras = data.get("extras", {})
                    recoverable = bool(
                        data.get("recoverable")
                        or extras.get("recoverable")
                    )
                    dispatch_not_started = bool(
                        data.get("dispatch_not_started")
                        or data.get("dispatch_spec_rejected")
                        or extras.get("dispatch_not_started")
                        or extras.get("dispatch_spec_rejected")
                    )
                    if dispatch_not_started:
                        approval_timeout = extras.get("dispatch_approval_timeout", False)
                        cancelled = extras.get("dispatch_cancelled", False)
                    else:
                        summary = data.get("summary", "")
                        needs_followup = bool(data.get("needs_followup", False))
                        status = data.get("status")
                except Exception:
                    pass

                # Finalize controller FIRST (updates planner/spec UI above)
                controller.finalize(ok, result_text)

                # THEN update spec card for not-started scenarios (stale/cancelled/expired).
                # Completed dispatches get one deduped final summary card so
                # live UI and persisted replay show the same terminal state.
                if dispatch_not_started:
                    spec_card = self.get_spec_card(tool_call_id)
                    if spec_card:
                        if approval_timeout:
                            spec_card.mark_dispatch_expired()
                        elif cancelled:
                            spec_card.mark_cancelled()
                        else:
                            spec_card.mark_stale()

                # Clean up any stale CodeWriterCards left in the tool cluster.
                # Worker write events go to the playground, not the main chat.
                # If a CodeWriterCard somehow ended up in the tool cluster in
                # Failed state, remove it so it doesn't appear as the final item.
                self._cleanup_failed_code_cards(tool_call_id)

                if summary and not dispatch_not_started and not (needs_followup and recoverable):
                    goal = (
                        controller.goal
                        or self._tool_goal_from_args(controller.buffer)
                        or "Worker task"
                    )
                    self.add_worker_summary(
                        tool_call_id,
                        goal,
                        ok,
                        summary,
                        needs_followup=needs_followup,
                        status=status,
                    )

            elif controller.tool_name == "run_research":
                report = ""
                needs_followup = False
                try:
                    data = json.loads(result_text)
                    report = data.get("report", "")
                    needs_followup = data.get("needs_followup", False)
                except Exception:
                    pass

                controller.finalize(ok, result_text)

                if report:
                    self.add_worker_summary(
                        tool_call_id, controller.goal or "Research", ok, report,
                        needs_followup=needs_followup,
                    )

            else:
                controller.finalize(ok, result_text)
                if controller.tool_name == "run_terminal_command":
                    self._terminal_cards.pop(tool_call_id, None)

            self._scroll_to_bottom()

    def _tool_goal_from_args(self, args_text: str) -> str:
        try:
            data = json.loads(args_text)
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("goal") or data.get("objective") or "").strip()

    def append_terminal_output(self, tool_call_id: str, text: str) -> None:
        """Append a chunk of stdout/stderr to the TerminalCard."""
        card = self._terminal_cards.get(tool_call_id)
        if card is not None:
            card.append_output(text)
        self._scroll_to_bottom()

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

    def add_error(self, title: str, message: str, show_retry: bool = False) -> None:
        card = ErrorCard(title, message, show_retry=show_retry, parent=self)
        if show_retry:
            card.retry_clicked.connect(self.retry_requested.emit)
        self._add_card(card)

    def stop_current_aura(self) -> None:
        if self._current_aura is not None:
            self._current_aura.stop_aura()

    def hold_aura_coding(self) -> None:
        """Keep the current aura alive in coding state (for dispatch transitions)."""
        if self._current_aura is not None:
            self._current_aura.set_glow_state("coding")

    def begin_planner_resolution_aura(self) -> None:
        """Start or restart the Planner resolution aura (thinking glow)."""
        if self._current_aura is not None:
            self._current_aura.set_glow_state("thinking")
        else:
            self.begin_assistant()

    def assistant_done(self) -> None:
        ac = self._current_assistant
        if ac is None:
            return
        ac.finalize_content()
        # Clean up any failed CodeWriterCards — write failures belong in
        # the Worker Log, not the main chat.  Call before stopping the aura
        # so the user sees a clean assistant summary as the final item.
        self._remove_failed_write_cards_from_assistant(ac)
        # Stop the breathing glow — content is complete, no need to pulse anymore.
        if self._current_aura is not None:
            self._current_aura.stop_aura()
        self._scroll_to_bottom()

    def finalize_markdown_only(self) -> None:
        """Finalize Markdown rendering without stopping the breathing aura.

        Use this when the stream has ended but the planner is still busy
        (e.g. waiting for dispatch resolution) so the aura should keep pulsing.
        """
        ac = self._current_assistant
        if ac is not None:
            ac.finalize_content()
            self._scroll_to_bottom()

    # ---- spec card / worker dispatch ------------------------------------

    def add_spec_card(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str = "",
    ) -> SpecCard:
        # Remove the in-flight plan writer card for this call ID (baton pass).
        self._remove_plan_writer_card(tool_call_id)

        existing = self._spec_cards.get(tool_call_id)
        if existing is not None:
            existing.update_spec(goal, files, spec, acceptance, summary)
            self._scroll_after_bottom_layout_change()
            return existing
        card = SpecCard(tool_call_id, goal, files, spec, acceptance, summary=summary, parent=self)
        ac = self.current_assistant()
        ac.add_footer_widget(card)
        self._spec_cards[tool_call_id] = card
        self._scroll_after_bottom_layout_change()
        return card

    def get_spec_card(self, tool_call_id: str) -> SpecCard | None:
        return self._spec_cards.get(tool_call_id)

    def remove_spec_card(self, tool_call_id: str) -> None:
        card = self._spec_cards.pop(tool_call_id, None)
        if card is None:
            return
        parent = card.parentWidget()
        if parent is not None:
            layout = parent.layout()
            if layout is not None:
                layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()
        self._scroll_after_bottom_layout_change()

    @property
    def worker_summary_disabled(self) -> bool:
        return self._worker_summary_disabled

    @worker_summary_disabled.setter
    def worker_summary_disabled(self, value: bool) -> None:
        self._worker_summary_disabled = value

    def add_worker_summary(
        self, tool_call_id: str, goal: str, ok: bool, summary: str,
        needs_followup: bool = False, status: str | None = None,
    ) -> None:
        """Add a summary card to the chat after a worker completes."""
        if self._worker_summary_disabled:
            self._remove_plan_writer_card(tool_call_id)
            return
        self._remove_plan_writer_card(tool_call_id)
        existing = self._worker_summary_cards.get(tool_call_id)
        if existing is not None:
            existing.update_summary(goal, ok, summary, needs_followup=needs_followup, status=status)
            self._scroll_after_bottom_layout_change()
            return
        card = WorkerSummaryCard(
            tool_call_id, goal, ok, summary,
            needs_followup=needs_followup, parent=self,
            status=status,
        )
        self._worker_summary_cards[tool_call_id] = card
        self._add_card(card)

    def add_mismatch_resolution_card(
        self, tool_call_id: str, kind: str = "", question: str = ""
    ) -> MismatchResolutionCard:
        """Add or update a mismatch resolution card for the given tool call."""
        existing = self._mismatch_resolution_cards.get(tool_call_id)
        if existing is not None:
            existing.update_mismatch(kind, question)
            return existing
        card = MismatchResolutionCard(tool_call_id, kind, question, parent=self)
        self._mismatch_resolution_cards[tool_call_id] = card
        self._add_card(card)
        return card

    def mark_mismatch_resolved(self, tool_call_id: str) -> None:
        """Mark a mismatch resolution card as resolved."""
        card = self._mismatch_resolution_cards.get(tool_call_id)
        if card is not None:
            card.mark_resolved()
