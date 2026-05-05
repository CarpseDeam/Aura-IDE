"""Main application window: three-pane splitter, toolbar, chat + input."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from aura.bridge import ConversationBridge
from aura.bridge.qt_bridge import PLANNER_SYSTEM_PROMPT
from aura.config import (
    APP_NAME,
    AppSettings,
    DEFAULT_MODEL,
    DEFAULT_THINKING,
    DEFAULT_WORKER_MODEL,
    DEFAULT_WORKER_THINKING,
    MODELS,
    ModelId,
    ThinkingMode,
    cost_usd,
    load_settings,
    load_workspace_root,
    save_settings,
    save_workspace_root,
)
from aura.conversation.persistence import (
    LoadedConversation,
    list_conversations,
    load_conversation,
    most_recent_conversation,
    save_conversation,
)
from aura.gui.chat_view import ChatView, SpecCard
from aura.gui.input_panel import Attachment, InputPanel, SendPayload
from aura.gui.settings_dialog import SettingsDialog
from aura.gui.spec_edit_dialog import SpecEditDialog
from aura.gui.theme import BG, BG_ALT, BORDER, FG, FG_DIM, FG_MUTED, WARN
from aura.gui.workspace_tree import WorkspaceTree

SYSTEM_PROMPT = (
    "You are Aura, a desktop assistant focused on troubleshooting code with the user.\n"
    "You have filesystem tools (read_file, list_directory, glob, write_file, edit_file) "
    "scoped to the user's workspace. Workspace-relative paths only.\n"
    "When the user asks about their code, USE the tools to read the actual files before "
    "answering — do not guess. When proposing changes, prefer edit_file with a tightly-"
    "scoped old_str (include enough surrounding context that it's unique) over write_file. "
    "Every write requires the user's approval through a diff dialog. If a write tool is "
    "not available, the user has enabled Read-Only Mode; explain what you would change "
    "instead. Be concise; show the user code, not prose, where it helps. Never fabricate "
    "file contents or call paths you have not verified with read_file."
)

_THINKING_LABEL = {"off": "Off", "high": "High", "max": "Max"}


def _toolbar_separator() -> QFrame:
    sep = QFrame()
    sep.setObjectName("toolbarSeparator")
    sep.setFrameShape(QFrame.Shape.VLine)
    sep.setFrameShadow(QFrame.Shadow.Plain)
    sep.setFixedWidth(1)
    return sep


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 900)

        # Settings.
        self._settings: AppSettings = load_settings()

        # Workspace.
        self._workspace_root: Path | None = load_workspace_root()
        if self._workspace_root is None:
            self._workspace_root = Path.cwd()

        # Bridge.
        self._bridge = ConversationBridge(parent_widget=self)
        self._bridge.set_workspace_root(self._workspace_root)
        self._apply_planner_worker_mode_to_bridge(self._settings.planner_worker_mode)
        self._bridge.set_worker_model(self._settings.default_worker_model)
        self._bridge.set_worker_thinking(self._settings.default_worker_thinking)

        # Persistence state.
        self._current_conversation_path: Path | None = None

        # Session usage accumulators (per-model so cost is exact when mixing).
        self._session_usage: dict[str, dict[str, int]] = {}

        # ----- toolbar ----
        self._toolbar = QToolBar("Main")
        self._toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._toolbar)
        self._build_toolbar()

        # ----- splitter ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)

        # Left pane: workspace label + change root + tree.
        self._left_pane = self._build_left_pane()
        splitter.addWidget(self._left_pane)

        # Right pane = chat view + input panel
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._chat = ChatView()
        right_layout.addWidget(self._chat, 1)

        self._input = InputPanel(self._workspace_root)
        # Apply default model / thinking from settings.
        if self._settings.planner_worker_mode:
            self._input.set_model(self._settings.default_planner_model)
            self._input.set_thinking(self._settings.default_planner_thinking)
        else:
            self._input.set_model(self._settings.default_model)
            self._input.set_thinking(self._settings.default_thinking)
        self._input.set_worker_model(self._settings.default_worker_model)
        self._input.set_worker_thinking(self._settings.default_worker_thinking)
        self._input.set_planner_worker_mode(self._settings.planner_worker_mode)
        right_layout.addWidget(self._input)

        splitter.addWidget(right)
        splitter.setSizes([280, 1120])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self.setCentralWidget(splitter)

        # ----- status bar -----
        self._build_status_bar()

        # ----- wire bridge ↔ view -----
        self._bridge.started.connect(self._on_started)
        self._bridge.finished.connect(self._on_finished)
        self._bridge.reasoningDelta.connect(self._chat.append_reasoning)
        self._bridge.contentDelta.connect(self._chat.append_content)
        self._bridge.toolCallStart.connect(self._chat.add_tool_call)
        self._bridge.toolCallArgs.connect(self._chat.append_tool_args)
        self._bridge.toolCallEnd.connect(lambda _id: None)
        self._bridge.toolResult.connect(self._on_tool_result)
        self._bridge.diffDecided.connect(self._on_diff_decided)
        self._bridge.streamDone.connect(self._on_stream_done)
        self._bridge.apiError.connect(self._on_api_error)
        self._bridge.usageWithModel.connect(self._on_usage)

        self._input.sent.connect(self._on_send)
        self._input.stop_requested.connect(self._on_stop)
        self._input.model_changed.connect(lambda _m: self._refresh_status_bar())
        self._input.thinking_changed.connect(lambda _t: self._refresh_status_bar())
        self._input.worker_model_changed.connect(self._on_worker_model_changed)
        self._input.worker_thinking_changed.connect(self._on_worker_thinking_changed)

        # Planner / worker dispatch flow.
        self._bridge.workerDispatchRequested.connect(self._on_worker_dispatch_requested)
        self._bridge.workerStarted.connect(self._on_worker_started)
        self._bridge.workerFinished.connect(self._on_worker_finished)
        self._bridge.workerCancelled.connect(self._on_worker_cancelled)
        self._bridge.workerReasoningDelta.connect(self._chat.worker_append_reasoning)
        self._bridge.workerContentDelta.connect(self._chat.worker_append_content)
        self._bridge.workerToolCallStart.connect(self._chat.worker_add_tool_call)
        self._bridge.workerToolCallArgs.connect(self._chat.worker_append_tool_args)
        self._bridge.workerToolCallEnd.connect(lambda _t, _w: None)
        self._bridge.workerToolResult.connect(self._on_worker_tool_result)
        self._bridge.workerDiffDecided.connect(self._on_worker_diff_decided)
        self._bridge.workerApiError.connect(self._on_worker_api_error)
        self._bridge.workerUsage.connect(self._on_worker_usage)

        self._update_workspace_label()
        self._refresh_status_bar()

        # Restore most recent conversation if enabled.
        if self._settings.restore_last_conversation:
            self._maybe_restore_last_conversation()

    # ----- toolbar build --------------------------------------------------

    def _build_toolbar(self) -> None:
        # Group 1: conversation actions
        new_act = QAction("New Conversation", self)
        new_act.triggered.connect(self._on_new_conversation)
        self._toolbar.addAction(new_act)

        open_act = QAction("Open Conversation...", self)
        open_act.triggered.connect(self._on_open_conversation)
        self._toolbar.addAction(open_act)

        self._toolbar.addWidget(_toolbar_separator())

        # Group 2: read-only
        self._read_only_act = QAction("Read-Only Mode", self)
        self._read_only_act.setCheckable(True)
        self._read_only_act.setChecked(False)
        self._read_only_act.triggered.connect(self._on_read_only_toggled)
        self._toolbar.addAction(self._read_only_act)

        self._read_only_badge = QLabel("")
        self._read_only_badge.setObjectName("readOnlyBadge")
        self._toolbar.addWidget(self._read_only_badge)

        self._toolbar.addWidget(_toolbar_separator())

        # Group 3: settings
        settings_act = QAction("Settings", self)
        settings_act.triggered.connect(self._on_open_settings)
        self._toolbar.addAction(settings_act)

        # Spacer.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(spacer)

        # Right side: workspace name (small)
        self._toolbar_workspace_label = QLabel("")
        self._toolbar_workspace_label.setStyleSheet(f"color: {FG_DIM};")
        self._toolbar.addWidget(self._toolbar_workspace_label)

    # ----- left pane ------------------------------------------------------

    def _build_left_pane(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("leftPane")
        frame.setMinimumWidth(220)
        frame.setMaximumWidth(420)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(4)

        title = QLabel("Workspace")
        title.setObjectName("paneTitle")
        layout.addWidget(title)

        self._workspace_label = QLabel("")
        self._workspace_label.setObjectName("workspaceLabel")
        self._workspace_label.setWordWrap(True)
        layout.addWidget(self._workspace_label)

        change_btn = QPushButton("Change Root...")
        change_btn.clicked.connect(self._on_change_root)
        # Bit of margin so the button doesn't hug the wall.
        change_row = QHBoxLayout()
        change_row.setContentsMargins(8, 0, 8, 6)
        change_row.addWidget(change_btn)
        layout.addLayout(change_row)

        self._tree = WorkspaceTree(self._workspace_root)
        layout.addWidget(self._tree, 1)

        return frame

    # ----- status bar -----------------------------------------------------

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        # Left side
        self._status_left = QLabel("")
        bar.addWidget(self._status_left, 1)
        # Right side
        self._status_tokens = QLabel("0 hit · 0 miss · 0 out")
        bar.addPermanentWidget(self._status_tokens)
        self._status_cost = QLabel("$0.0000")
        self._status_cost.setObjectName("statusCost")
        bar.addPermanentWidget(self._status_cost)

    def _refresh_status_bar(self) -> None:
        # Left: workspace path (truncated), model, thinking
        ws = str(self._workspace_root) if self._workspace_root else "(none)"
        if len(ws) > 64:
            ws = "…" + ws[-63:]
        model_label = MODELS[self._input.current_model()].label
        thinking_label = _THINKING_LABEL[self._input.current_thinking()]
        self._status_left.setText(f"{ws}    ·    {model_label}    ·    Thinking: {thinking_label}")

        # Right: totals + cost (sum across models)
        total_hit = sum(u["hit"] for u in self._session_usage.values())
        total_miss = sum(u["miss"] for u in self._session_usage.values())
        total_out = sum(u["out"] for u in self._session_usage.values())
        total_cost = 0.0
        for model_id, u in self._session_usage.items():
            try:
                total_cost += cost_usd(model_id, u["hit"], u["miss"], u["out"])  # type: ignore[arg-type]
            except KeyError:
                pass
        self._status_tokens.setText(
            f"{total_hit:,} hit · {total_miss:,} miss · {total_out:,} out"
        )
        self._status_cost.setText(f"${total_cost:.4f}")

    def _reset_session_usage(self) -> None:
        self._session_usage.clear()
        self._refresh_status_bar()

    # ----- handlers -------------------------------------------------------

    def _on_change_root(self) -> None:
        start = str(self._workspace_root) if self._workspace_root else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose workspace root", start)
        if not chosen:
            return
        path = Path(chosen)
        self._workspace_root = path
        self._bridge.set_workspace_root(path)
        self._input.set_workspace_root(path)
        self._tree.set_root(path)
        save_workspace_root(path)
        # New workspace — drop any current conversation pointer (different .aura/).
        self._current_conversation_path = None
        self._update_workspace_label()
        self._refresh_status_bar()

    def _update_workspace_label(self) -> None:
        if self._workspace_root is None:
            self._workspace_label.setText("(none)")
            self._toolbar_workspace_label.setText("")
            return
        full = str(self._workspace_root)
        self._workspace_label.setText(full)
        self._toolbar_workspace_label.setText(self._workspace_root.name)

    def _on_read_only_toggled(self, checked: bool) -> None:
        self._bridge.set_read_only(checked)
        if checked:
            self._read_only_act.setText("\U0001F512 Read-Only Mode")  # lock
            self._read_only_badge.setText("READ-ONLY")
        else:
            self._read_only_act.setText("Read-Only Mode")
            self._read_only_badge.setText("")

    def _on_new_conversation(self) -> None:
        if self._bridge.is_running():
            QMessageBox.information(
                self, APP_NAME, "Wait for the current response to finish, or click Stop."
            )
            return
        self._bridge.reset_history()
        self._chat.reset()
        self._current_conversation_path = None
        self._reset_session_usage()

    def _on_open_conversation(self) -> None:
        if self._bridge.is_running():
            QMessageBox.information(
                self, APP_NAME, "Wait for the current response to finish, or click Stop."
            )
            return
        if self._workspace_root is None:
            return
        start = str(self._workspace_root / ".aura" / "conversations")
        Path(start).mkdir(parents=True, exist_ok=True)
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Open Conversation", start, "Conversations (*.json)"
        )
        if not chosen:
            return
        try:
            loaded = load_conversation(Path(chosen))
        except Exception as exc:
            QMessageBox.warning(
                self, APP_NAME, f"Could not open conversation:\n{exc}"
            )
            return
        self._apply_loaded_conversation(loaded)

    def _on_open_settings(self) -> None:
        dlg = SettingsDialog(
            settings=self._settings,
            workspace_root=self._workspace_root,
            on_change_root=self._on_change_root,
            parent=self,
        )
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self._settings = dlg.result_settings()
            # Apply to current widgets.
            if self._settings.planner_worker_mode:
                self._input.set_model(self._settings.default_planner_model)
                self._input.set_thinking(self._settings.default_planner_thinking)
            else:
                self._input.set_model(self._settings.default_model)
                self._input.set_thinking(self._settings.default_thinking)
            self._input.set_worker_model(self._settings.default_worker_model)
            self._input.set_worker_thinking(self._settings.default_worker_thinking)
            self._input.set_planner_worker_mode(self._settings.planner_worker_mode)
            self._apply_planner_worker_mode_to_bridge(self._settings.planner_worker_mode)
            self._bridge.set_worker_model(self._settings.default_worker_model)
            self._bridge.set_worker_thinking(self._settings.default_worker_thinking)
            self._refresh_status_bar()

    def _apply_planner_worker_mode_to_bridge(self, enabled: bool) -> None:
        self._bridge.set_planner_worker_mode(enabled)
        self._bridge.set_system_prompt(
            PLANNER_SYSTEM_PROMPT if enabled else SYSTEM_PROMPT
        )

    def _on_worker_model_changed(self, model: str) -> None:
        self._bridge.set_worker_model(model)  # type: ignore[arg-type]
        self._refresh_status_bar()

    def _on_worker_thinking_changed(self, thinking: str) -> None:
        self._bridge.set_worker_thinking(thinking)  # type: ignore[arg-type]
        self._refresh_status_bar()

    def _on_send(self, payload: SendPayload) -> None:
        if self._bridge.is_running():
            return
        # Prepare history append: image attachments go via multimodal content array.
        text = payload.text
        # Add text refs from non-image attachments to the text body so the model knows.
        text_refs = [a.text_ref for a in payload.attachments if a.text_ref]
        if text_refs:
            ref_block = "\n".join(text_refs)
            text = f"{text}\n\n{ref_block}".strip() if text else ref_block
        image_atts = [a for a in payload.attachments if a.kind == "image" and a.b64]

        if image_atts:
            parts = []
            if text:
                parts.append({"type": "text", "text": text})
            for a in image_atts:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{a.b64}"},
                    }
                )
            self._bridge.history.append_user_multimodal(parts)
        else:
            self._bridge.history.append_user_text(text)

        self._chat.add_user(text, [a.b64 for a in image_atts] or None)
        self._chat.begin_assistant()

        self._bridge.send(
            model=self._input.current_model(),
            thinking=self._input.current_thinking(),
        )

    def _on_stop(self) -> None:
        self._bridge.request_cancel()

    def _on_started(self) -> None:
        self._input.set_streaming(True)

    def _on_finished(self) -> None:
        self._input.set_streaming(False)
        self._chat.assistant_done()
        self._input.focus_editor()

    def _on_stream_done(self, finish_reason: str, full_message: dict) -> None:
        # Render markdown for the final answer.
        self._chat.assistant_done()
        # Auto-save after each assistant turn — including partial tool-call rounds.
        self._auto_save_conversation()

    def _on_tool_result(self, tool_id: str, name: str, ok: bool, result: str, extras: dict) -> None:
        self._chat.set_tool_result(tool_id, ok, result)

    def _on_diff_decided(
        self,
        tool_call_id: str,
        decision: str,
        rel_path: str,
        old: str,
        new: str,
        is_new_file: bool,
    ) -> None:
        self._chat.add_diff_card(tool_call_id, rel_path, old, new, decision, is_new_file)

    # ---- planner/worker dispatch slots -----------------------------------

    def _on_worker_dispatch_requested(
        self,
        tool_call_id: str,
        goal: str,
        files: list,
        spec: str,
        acceptance: str,
    ) -> None:
        card = self._chat.add_spec_card(
            tool_call_id, goal, list(files), spec, acceptance
        )
        card.dispatch_clicked.connect(self._on_dispatch_clicked)
        card.edit_clicked.connect(self._on_edit_spec_clicked)
        card.cancel_clicked.connect(self._on_cancel_dispatch_clicked)

    def _on_dispatch_clicked(self, tool_call_id: str) -> None:
        card = self._chat.get_spec_card(tool_call_id)
        if card is None:
            return
        goal, files, spec, acceptance = card.current_spec()
        self._bridge.user_dispatched(tool_call_id, goal, files, spec, acceptance)

    def _on_edit_spec_clicked(self, tool_call_id: str) -> None:
        card = self._chat.get_spec_card(tool_call_id)
        if card is None:
            return
        goal, files, spec, acceptance = card.current_spec()
        dlg = SpecEditDialog(goal, files, spec, acceptance, parent=self)
        if dlg.exec() == SpecEditDialog.DialogCode.Accepted:
            card.update_spec(dlg.goal(), dlg.files(), dlg.spec(), dlg.acceptance())

    def _on_cancel_dispatch_clicked(self, tool_call_id: str) -> None:
        self._bridge.user_cancelled_dispatch(tool_call_id)

    def _on_worker_started(self, _tool_call_id: str) -> None:
        return

    def _on_worker_finished(self, tool_call_id: str, ok: bool, summary: str) -> None:
        self._chat.worker_finished(tool_call_id, ok, summary)

    def _on_worker_cancelled(self, tool_call_id: str) -> None:
        card = self._chat.get_spec_card(tool_call_id)
        if card is not None:
            card.disable_buttons()

    def _on_worker_tool_result(
        self,
        parent_tool_id: str,
        worker_tool_id: str,
        name: str,
        ok: bool,
        result: str,
        extras: dict,
    ) -> None:
        self._chat.worker_set_tool_result(parent_tool_id, worker_tool_id, ok, result)

    def _on_worker_diff_decided(
        self,
        parent_tool_id: str,
        worker_tool_id: str,
        decision: str,
        rel_path: str,
        old: str,
        new: str,
        is_new_file: bool,
    ) -> None:
        self._chat.worker_add_diff_card(
            parent_tool_id, worker_tool_id, rel_path, old, new, decision, is_new_file
        )

    def _on_worker_api_error(self, tool_call_id: str, status: int, message: str) -> None:
        title = f"API Error {status}" if status > 0 else "Worker Error"
        self._chat.worker_add_error(tool_call_id, f"{title}: {message}")

    def _on_worker_usage(
        self,
        _tool_call_id: str,
        model_id: str,
        prompt: int,
        completion: int,
        hit: int,
        miss: int,
    ) -> None:
        if hit == 0 and miss == 0:
            miss = prompt
        bucket = self._session_usage.setdefault(
            model_id, {"hit": 0, "miss": 0, "out": 0}
        )
        bucket["hit"] += hit
        bucket["miss"] += miss
        bucket["out"] += completion
        self._refresh_status_bar()

    def _on_api_error(self, status: int, message: str) -> None:
        title = f"API Error {status}" if status > 0 else "Error"
        self._chat.add_error(title, message, show_retry=True)

    def _on_retry(self) -> None:
        if self._bridge.is_running():
            return
        self._chat.begin_assistant()
        self._bridge.send(
            model=self._input.current_model(),
            thinking=self._input.current_thinking(),
        )

    def _on_usage(
        self, model_id: str, prompt: int, completion: int, hit: int, miss: int
    ) -> None:
        # Some servers don't surface the cache split — fall back so we still meter cost.
        if hit == 0 and miss == 0:
            miss = prompt
        bucket = self._session_usage.setdefault(
            model_id, {"hit": 0, "miss": 0, "out": 0}
        )
        bucket["hit"] += hit
        bucket["miss"] += miss
        bucket["out"] += completion
        self._refresh_status_bar()

    # ----- persistence ----------------------------------------------------

    def _auto_save_conversation(self) -> None:
        if self._workspace_root is None:
            return
        if not self._bridge.history.messages:
            return
        try:
            self._current_conversation_path = save_conversation(
                history=self._bridge.history,
                workspace_root=self._workspace_root,
                model=self._input.current_model(),
                thinking=self._input.current_thinking(),
                existing_path=self._current_conversation_path,
                planner_worker_mode=self._bridge.planner_worker_mode,
                planner_model=self._input.current_model(),
                worker_model=self._input.current_worker_model(),
                planner_thinking=self._input.current_thinking(),
                worker_thinking=self._input.current_worker_thinking(),
                worker_dispatches=self._bridge.dispatch_records,
            )
        except OSError as exc:
            # Disk error — surface but don't crash the chat.
            self._chat.add_error("Could not save conversation", str(exc))

    def _maybe_restore_last_conversation(self) -> None:
        if self._workspace_root is None:
            return
        path = most_recent_conversation(self._workspace_root)
        if path is None:
            return
        try:
            loaded = load_conversation(path)
        except Exception:
            return
        self._apply_loaded_conversation(loaded)

    def _apply_loaded_conversation(self, loaded: LoadedConversation) -> None:
        pwm = loaded.planner_worker_mode
        default_prompt = PLANNER_SYSTEM_PROMPT if pwm else SYSTEM_PROMPT
        self._bridge.history.system_prompt = (
            loaded.history.system_prompt or default_prompt
        )
        self._bridge.history.messages = list(loaded.history.messages)
        self._current_conversation_path = loaded.path
        self._reset_session_usage()
        # Sync mode (without overwriting the system prompt we just set).
        self._bridge.set_planner_worker_mode(pwm)
        if pwm:
            self._input.set_model(loaded.planner_model)
            self._input.set_thinking(loaded.planner_thinking)
            self._input.set_worker_model(loaded.worker_model)
            self._input.set_worker_thinking(loaded.worker_thinking)
            self._bridge.set_worker_model(loaded.worker_model)
            self._bridge.set_worker_thinking(loaded.worker_thinking)
        else:
            self._input.set_model(loaded.model)
            self._input.set_thinking(loaded.thinking)
        self._input.set_planner_worker_mode(pwm)
        self._chat.reset()
        self._replay_history_into_view()
        self._refresh_status_bar()

    def _replay_history_into_view(self) -> None:
        """Best-effort visual replay of a loaded history.

        We intentionally don't try to recreate diff cards (the underlying
        before/after content isn't stored — only the resulting tool-message
        from the registry). Tool calls are surfaced as cards with their
        recorded args + result so the conversation reads coherently.
        """
        msgs = self._bridge.history.messages
        # Index tool results by tool_call_id for inline pairing.
        tool_results: dict[str, str] = {}
        for m in msgs:
            if m.get("role") == "tool":
                tcid = m.get("tool_call_id")
                if isinstance(tcid, str):
                    tool_results[tcid] = m.get("content", "")

        for m in msgs:
            role = m.get("role")
            if role == "user":
                content = m.get("content")
                if isinstance(content, str):
                    self._chat.add_user(content)
                elif isinstance(content, list):
                    text_parts = [
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    self._chat.add_user("\n".join(text_parts))
            elif role == "assistant":
                self._chat.begin_assistant()
                rc = m.get("reasoning_content")
                if rc:
                    self._chat.append_reasoning(rc)
                content = m.get("content")
                if isinstance(content, str) and content:
                    self._chat.append_content(content)
                for tc in m.get("tool_calls") or []:
                    tcid = tc.get("id", "")
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", "")
                    self._chat.add_tool_call(tcid, name)
                    if args:
                        self._chat.append_tool_args(tcid, args)
                    if tcid in tool_results:
                        # Determine ok by parsing the recorded JSON if possible.
                        ok = True
                        try:
                            parsed = json.loads(tool_results[tcid])
                            if isinstance(parsed, dict) and parsed.get("ok") is False:
                                ok = False
                        except json.JSONDecodeError:
                            pass
                        self._chat.set_tool_result(tcid, ok, tool_results[tcid])
                # Finalize markdown for content if present.
                self._chat.assistant_done()
            # tool messages are paired into the assistant cards above
