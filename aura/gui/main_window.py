"""Main application window: three-pane splitter, toolbar, chat + input."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.bridge import ConversationBridge
from aura.bridge.qt_bridge import PLANNER_SYSTEM_PROMPT
from aura.config import (
    APP_NAME,
    PROVIDERS,
    AppSettings,
    DEFAULT_THINKING,
    DEFAULT_WORKER_THINKING,
    ProviderId,
    ThinkingMode,
    cost_usd,
    icon_path,
    load_settings,
    load_workspace_root,
    media_path,
    save_workspace_root,
)
from aura.conversation.persistence import (
    LoadedConversation,
    load_conversation,
    most_recent_conversation,
    save_conversation,
)
from aura.gui.chat_view import ChatView
from aura.gui.input_panel import InputPanel, SendPayload
from aura.gui.settings_dialog import SettingsDialog
from aura.gui.spec_edit_dialog import SpecApprovalDialog, SpecEditDialog
from aura.gui.theme import BORDER, FG_DIM
from aura.gui.worker_window import AuraPlayground
from aura.gui.workspace_tree import WorkspaceTree

SYSTEM_PROMPT = (
    "You are Aura, a desktop assistant focused on troubleshooting code with the user.\n"
    "You have filesystem tools (read_file, list_directory, glob, write_file, edit_file) "
    "scoped to the user's workspace. Workspace-relative paths only.\n"
    "When the user asks about their code, USE the tools to read the actual files before "
    "answering — do not guess. When proposing changes, prefer edit_file with a "
    "Search Block (the code to change plus a few lines of surrounding context) over write_file. "
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
        self.setWindowIcon(QIcon(str(icon_path())))
        self.resize(1400, 900)

        # Drag-to-move state
        self._dragging = False
        self._drag_start_pos = None

        # Settings.
        self._settings: AppSettings = load_settings()

        # Workspace.
        self._workspace_root: Path | None = load_workspace_root()
        if self._workspace_root is None:
            self._workspace_root = Path.cwd()

        # Bridge — provider-aware.
        self._bridge = ConversationBridge(
            parent_widget=self,
            provider=self._settings.provider,
        )
        self._bridge.set_workspace_root(self._workspace_root)
        self._apply_planner_worker_mode_to_bridge(self._settings.planner_worker_mode)
        self._bridge.set_worker_model(self._settings.default_worker_model)
        self._bridge.set_worker_thinking(self._settings.default_worker_thinking)
        self._bridge.set_temperature(self._settings.temperature)
        self._bridge.set_worker_temperature(self._settings.worker_temperature)
        self._bridge.set_custom_system_prompts(
            self._settings.system_prompt,
            self._settings.planner_system_prompt,
            self._settings.worker_system_prompt,
        )

        # Persistence state.
        self._current_conversation_path: Path | None = None

        # Session usage accumulators (per-model so cost is exact when mixing).
        self._session_usage: dict[str, dict[str, int]] = {}

        # Queued messages sent while worker is running.
        self._message_queue: list[SendPayload] = []

        # ----- toolbar ----
        self._toolbar = QToolBar("Main")
        self._toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._toolbar)
        self._build_toolbar()

        # ----- status bar -----
        self._build_status_bar()

        # ----- splitter ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)

        # Left pane: workspace label + change root + tree.
        self._left_pane = self._build_left_pane()
        splitter.addWidget(self._left_pane)

        # Middle pane: chat + input
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(20, 0, 20, 16)
        center_layout.setSpacing(0)

        self._chat = ChatView()
        if self._settings.planner_worker_mode:
            self._chat.set_compact_tools(True)
        center_layout.addWidget(self._chat, 1)

        self._input = InputPanel(self._workspace_root)
        # Apply default model / thinking from settings.
        if self._settings.planner_worker_mode:
            self.set_model(self._settings.default_planner_model)
            self.set_thinking(self._settings.default_planner_thinking)
        else:
            self.set_model(self._settings.default_model)
            self.set_thinking(self._settings.default_thinking)
        self.set_worker_model(self._settings.default_worker_model)
        self.set_worker_thinking(self._settings.default_worker_thinking)
        self._set_sidebar_planner_worker_mode(self._settings.planner_worker_mode)
        center_layout.addWidget(self._input)

        splitter.addWidget(center)

        # Right pane: worker activity (embedded, not a separate window)
        self._playground = AuraPlayground(parent=self)
        splitter.addWidget(self._playground)

        w = self.width()
        splitter.setSizes([min(200, w // 8), (w - min(200, w // 8)) // 2, (w - min(200, w // 8)) // 2])
        splitter.setStretchFactor(0, 0)  # workspace tree doesn't stretch
        splitter.setStretchFactor(1, 1)  # chat gets 1/2 of stretch
        splitter.setStretchFactor(2, 1)  # worker gets 1/2 of stretch

        self.setCentralWidget(splitter)

        # Make the central widget and splitter transparent so the gradient shows through
        splitter.setStyleSheet("background: transparent;")
        self.centralWidget().setStyleSheet("background: transparent;")
        self.centralWidget().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        # Frameless window — no native title bar
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)

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

        # Planner / worker dispatch flow.
        self._bridge.workerDispatchRequested.connect(self._on_worker_dispatch_requested)
        self._bridge.workerStarted.connect(self._on_worker_started)
        self._bridge.workerFinished.connect(self._on_worker_finished)
        self._bridge.workerCancelled.connect(self._on_worker_cancelled)
        self._bridge.workerReasoningDelta.connect(self._on_worker_reasoning)
        self._bridge.workerContentDelta.connect(self._on_worker_content)
        self._bridge.workerToolCallStart.connect(self._on_worker_tool_call_start)
        self._bridge.workerToolCallArgs.connect(self._on_worker_tool_args)
        self._bridge.workerToolCallEnd.connect(lambda _t, _w: None)
        self._bridge.workerToolResult.connect(self._on_worker_tool_result)
        self._bridge.workerDiffDecided.connect(self._on_worker_diff_decided)
        self._bridge.workerApiError.connect(self._on_worker_api_error)
        self._bridge.workerUsage.connect(self._on_worker_usage)
        self._bridge.workerTodoListUpdated.connect(self._on_worker_todo_list_updated)
        self._bridge.workerTerminalOutput.connect(self._on_worker_terminal_output)
        self._bridge.terminalOutput.connect(self._on_terminal_output)

        # Mermaid diagram detection from chat → playground
        self._chat.mermaid_detected.connect(self._playground.add_mermaid_artifact)

        self._update_workspace_label()
        self._refresh_status_bar()

        # Restore most recent conversation if enabled.
        if self._settings.restore_last_conversation:
            self._maybe_restore_last_conversation()

    # ----- paintEvent: radial gradient background --------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        center.setY(int(self.height() * 0.15))
        radius = max(self.width(), self.height()) * 0.8
        gradient = QRadialGradient(center, radius)
        gradient.setColorAt(0.0, QColor(30, 34, 46, 255))
        gradient.setColorAt(0.4, QColor(18, 20, 26, 255))
        gradient.setColorAt(1.0, QColor(6, 8, 12, 255))
        painter.fillRect(self.rect(), gradient)
        painter.end()
        super().paintEvent(event)

    # ----- drag-to-move on toolbar -----------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            tb_geo = self._toolbar.geometry()
            if tb_geo.contains(event.position().toPoint()):
                # Don't drag if clicking on a toolbar button
                child = self._toolbar.childAt(self._toolbar.mapFrom(self, event.position().toPoint()))
                if child is not None and isinstance(child, QToolButton):
                    super().mousePressEvent(event)
                    return
                self._drag_start_pos = event.globalPosition().toPoint()
                self._dragging = True
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, '_dragging', False):
            delta = event.globalPosition().toPoint() - self._drag_start_pos
            self.move(self.pos() + delta)
            self._drag_start_pos = event.globalPosition().toPoint()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if getattr(self, '_dragging', False):
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ----- toolbar build --------------------------------------------------

    def _build_toolbar(self) -> None:
        # Group 1: conversation actions
        new_act = QAction(QIcon(str(media_path("new_conv.svg"))), "New Conversation", self)
        new_act.triggered.connect(self._on_new_conversation)
        self._toolbar.addAction(new_act)

        open_act = QAction(QIcon(str(media_path("open_conversation.svg"))), "Open Conversation...", self)
        open_act.triggered.connect(self._on_open_conversation)
        self._toolbar.addAction(open_act)

        self._toolbar.addWidget(_toolbar_separator())

        # Group 2: read-only
        self._read_only_act = QAction(QIcon(str(media_path("read_only.svg"))), "Read-Only Mode", self)
        self._read_only_act.setCheckable(True)
        self._read_only_act.setChecked(False)
        self._read_only_act.triggered.connect(self._on_read_only_toggled)
        self._toolbar.addAction(self._read_only_act)

        self._read_only_badge = QLabel("")
        self._read_only_badge.setObjectName("readOnlyBadge")
        self._toolbar.addWidget(self._read_only_badge)

        self._toolbar.addWidget(_toolbar_separator())

        # Group 3: settings
        settings_act = QAction(QIcon(str(media_path("settings_24dp.svg"))), "Settings", self)
        settings_act.triggered.connect(self._on_open_settings)
        self._toolbar.addAction(settings_act)

        # Icon-only style.
        self._toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        # Spacer.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(spacer)

        # Right side: workspace name (small)
        self._toolbar_workspace_label = QLabel("")
        self._toolbar_workspace_label.setStyleSheet(f"color: {FG_DIM};")
        self._toolbar.addWidget(self._toolbar_workspace_label)

        # Small spacer before window controls.
        win_spacer = QWidget()
        win_spacer.setFixedWidth(8)
        self._toolbar.addWidget(win_spacer)

        # Window control buttons.
        min_btn = QToolButton()
        min_btn.setText("\u2500")  # ─
        min_btn.setObjectName("winMinBtn")
        min_btn.clicked.connect(self.showMinimized)
        self._toolbar.addWidget(min_btn)

        self._max_btn = QToolButton()
        self._max_btn.setText("\u25a1")  # □
        self._max_btn.setObjectName("winMaxBtn")
        self._max_btn.clicked.connect(self._toggle_maximize)
        self._toolbar.addWidget(self._max_btn)

        close_btn = QToolButton()
        close_btn.setText("\u2715")  # ✕
        close_btn.setObjectName("winCloseBtn")
        close_btn.clicked.connect(self.close)
        self._toolbar.addWidget(close_btn)

    # ----- window state helpers -------------------------------------------

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
            self._max_btn.setText("\u25a1")  # □
        else:
            self.showMaximized()
            self._max_btn.setText("\u2750")  # ❐

    def changeEvent(self, event) -> None:
        if event.type() == event.Type.WindowStateChange:
            if hasattr(self, '_max_btn'):
                if self.isMaximized():
                    self._max_btn.setText("\u2750")  # ❐
                else:
                    self._max_btn.setText("\u25a1")  # □
        super().changeEvent(event)

    # ----- left pane ------------------------------------------------------

    def _build_left_pane(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("leftPane")
        frame.setMinimumWidth(160)
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

        # --- Model Config section ---
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"QFrame {{ color: {BORDER}; }}")
        layout.addWidget(sep)

        model_label = QLabel("Model Configuration")
        model_label.setObjectName("paneTitle")
        layout.addWidget(model_label)

        # Planner model
        planner_model_row = QHBoxLayout()
        planner_model_row.setSpacing(4)
        planner_model_label = QLabel("Planner:")
        planner_model_label.setStyleSheet(f"color: {FG_DIM};")
        planner_model_row.addWidget(planner_model_label)
        self._planner_model_combo = QComboBox()
        self._worker_model_combo = QComboBox()
        self._populate_model_combos(self._settings.provider)
        self._planner_model_combo.setCurrentIndex(-1)  # will be set below
        self._planner_model_combo.currentIndexChanged.connect(self._refresh_status_bar)
        planner_model_row.addWidget(self._planner_model_combo, 1)
        layout.addLayout(planner_model_row)

        # Planner thinking
        planner_think_row = QHBoxLayout()
        planner_think_row.setSpacing(4)
        planner_think_label = QLabel("Thinking:")
        planner_think_label.setStyleSheet(f"color: {FG_DIM};")
        planner_think_row.addWidget(planner_think_label)
        self._planner_thinking_combo = QComboBox()
        self._planner_thinking_combo.addItem("Off", "off")
        self._planner_thinking_combo.addItem("High", "high")
        self._planner_thinking_combo.addItem("Max", "max")
        self._planner_thinking_combo.setCurrentIndex(["off", "high", "max"].index(DEFAULT_THINKING))
        self._planner_thinking_combo.currentIndexChanged.connect(self._refresh_status_bar)
        planner_think_row.addWidget(self._planner_thinking_combo, 1)
        layout.addLayout(planner_think_row)

        # Worker model
        worker_model_row = QHBoxLayout()
        worker_model_row.setSpacing(4)
        self._worker_model_label = QLabel("Worker:")
        self._worker_model_label.setStyleSheet(f"color: {FG_DIM};")
        worker_model_row.addWidget(self._worker_model_label)
        self._worker_model_combo.setCurrentIndex(-1)  # will be set below
        self._worker_model_combo.currentIndexChanged.connect(self._on_sidebar_worker_model_changed)
        worker_model_row.addWidget(self._worker_model_combo, 1)
        layout.addLayout(worker_model_row)

        # Worker thinking
        worker_think_row = QHBoxLayout()
        worker_think_row.setSpacing(4)
        self._worker_thinking_label = QLabel("Thinking:")
        self._worker_thinking_label.setStyleSheet(f"color: {FG_DIM};")
        worker_think_row.addWidget(self._worker_thinking_label)
        self._worker_thinking_combo = QComboBox()
        self._worker_thinking_combo.addItem("Off", "off")
        self._worker_thinking_combo.addItem("High", "high")
        self._worker_thinking_combo.addItem("Max", "max")
        self._worker_thinking_combo.setCurrentIndex(["off", "high", "max"].index(DEFAULT_WORKER_THINKING))
        self._worker_thinking_combo.currentIndexChanged.connect(self._on_sidebar_worker_thinking_changed)
        worker_think_row.addWidget(self._worker_thinking_combo, 1)
        layout.addLayout(worker_think_row)

        return frame

    # ----- provider-aware model combo helpers -----------------------------

    def _populate_model_combos(
        self,
        provider_id: ProviderId,
        combo: QComboBox | None = None,
    ) -> None:
        """Fill a model combo (or both planner & worker) from the provider's catalog."""
        cfg = PROVIDERS[provider_id]
        if combo is not None:
            combo.blockSignals(True)
            combo.clear()
            for mid, info in cfg.models.items():
                combo.addItem(info.label, mid)
            combo.blockSignals(False)
        else:
            # Fill both combos
            self._planner_model_combo.blockSignals(True)
            self._planner_model_combo.clear()
            for mid, info in cfg.models.items():
                self._planner_model_combo.addItem(info.label, mid)
            self._planner_model_combo.blockSignals(False)

            self._worker_model_combo.blockSignals(True)
            self._worker_model_combo.clear()
            for mid, info in cfg.models.items():
                self._worker_model_combo.addItem(info.label, mid)
            self._worker_model_combo.blockSignals(False)

    def _model_label(self, model_id: str) -> str:
        """Look up a model's human-readable label from any provider."""
        for cfg in PROVIDERS.values():
            if model_id in cfg.models:
                return cfg.models[model_id].label
        return model_id

    # ----- model / thinking accessors ------------------------------------

    def current_model(self) -> str:
        return self._planner_model_combo.currentData()

    def current_thinking(self) -> ThinkingMode:
        return self._planner_thinking_combo.currentData()

    def current_worker_model(self) -> str:
        return self._worker_model_combo.currentData()

    def current_worker_thinking(self) -> ThinkingMode:
        return self._worker_thinking_combo.currentData()

    def set_model(self, model: str) -> None:
        idx = self._planner_model_combo.findData(model)
        if idx >= 0:
            self._planner_model_combo.setCurrentIndex(idx)

    def set_thinking(self, thinking: ThinkingMode) -> None:
        keys = ["off", "high", "max"]
        if thinking in keys:
            self._planner_thinking_combo.setCurrentIndex(keys.index(thinking))

    def set_worker_model(self, model: str) -> None:
        idx = self._worker_model_combo.findData(model)
        if idx >= 0:
            self._worker_model_combo.setCurrentIndex(idx)

    def set_worker_thinking(self, thinking: ThinkingMode) -> None:
        keys = ["off", "high", "max"]
        if thinking in keys:
            self._worker_thinking_combo.setCurrentIndex(keys.index(thinking))

    def _on_sidebar_worker_model_changed(self, _index: int) -> None:
        self._bridge.set_worker_model(self.current_worker_model())
        self._refresh_status_bar()

    def _on_sidebar_worker_thinking_changed(self, _index: int) -> None:
        self._bridge.set_worker_thinking(self.current_worker_thinking())
        self._refresh_status_bar()

    def _set_sidebar_planner_worker_mode(self, enabled: bool) -> None:
        self._worker_model_label.setVisible(enabled)
        self._worker_model_combo.setVisible(enabled)
        self._worker_thinking_label.setVisible(enabled)
        self._worker_thinking_combo.setVisible(enabled)

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

        # Monospace for numbers — prevents jitter as digit widths change.
        mono_font = QFont("Geist Mono, JetBrains Mono, Consolas, monospace")
        mono_font.setStyleHint(QFont.StyleHint.Monospace)
        mono_font.setPointSize(11)
        self._status_tokens.setFont(mono_font)
        self._status_cost.setFont(mono_font)

    def _refresh_status_bar(self) -> None:
        # Left: workspace path (truncated), model, thinking
        ws = str(self._workspace_root) if self._workspace_root else "(none)"
        if len(ws) > 64:
            ws = "…" + ws[-63:]
        model_label = self._model_label(self.current_model())
        thinking_label = _THINKING_LABEL.get(self.current_thinking(), "Off")
        self._status_left.setText(f"{ws}    ·    {model_label}    ·    Thinking: {thinking_label}")

        # Right: totals + cost (sum across models)
        total_hit = sum(u["hit"] for u in self._session_usage.values())
        total_miss = sum(u["miss"] for u in self._session_usage.values())
        total_out = sum(u["out"] for u in self._session_usage.values())
        total_cost = 0.0
        for model_id, u in self._session_usage.items():
            try:
                total_cost += cost_usd(model_id, u["hit"], u["miss"], u["out"])
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
        self._playground.clear()
        self._message_queue.clear()
        self._input.set_queued_messages(0)

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
            old_provider = self._settings.provider
            self._settings = dlg.result_settings()
            # If provider changed, refresh model combos and recreate the bridge client.
            if self._settings.provider != old_provider:
                self._populate_model_combos(self._settings.provider)
                self._bridge.set_provider(self._settings.provider)
            # Apply to current widgets.
            if self._settings.planner_worker_mode:
                self.set_model(self._settings.default_planner_model)
                self.set_thinking(self._settings.default_planner_thinking)
            else:
                self.set_model(self._settings.default_model)
                self.set_thinking(self._settings.default_thinking)
            self.set_worker_model(self._settings.default_worker_model)
            self.set_worker_thinking(self._settings.default_worker_thinking)
            self._set_sidebar_planner_worker_mode(self._settings.planner_worker_mode)
            self._apply_planner_worker_mode_to_bridge(self._settings.planner_worker_mode)
            self._bridge.set_worker_model(self._settings.default_worker_model)
            self._bridge.set_worker_thinking(self._settings.default_worker_thinking)
            self._bridge.set_temperature(self._settings.temperature)
            self._bridge.set_worker_temperature(self._settings.worker_temperature)
            self._bridge.set_custom_system_prompts(
                self._settings.system_prompt,
                self._settings.planner_system_prompt,
                self._settings.worker_system_prompt,
            )
            self._refresh_status_bar()

    def _apply_planner_worker_mode_to_bridge(self, enabled: bool) -> None:
        self._bridge.set_planner_worker_mode(enabled)
        if enabled:
            prompt = self._settings.planner_system_prompt or PLANNER_SYSTEM_PROMPT
        else:
            prompt = self._settings.system_prompt or SYSTEM_PROMPT
        self._bridge.set_system_prompt(prompt)
        if hasattr(self, "_chat"):
            self._chat.set_compact_tools(enabled)

    def _on_worker_model_changed(self, model: str) -> None:
        self._bridge.set_worker_model(model)
        self._refresh_status_bar()

    def _on_worker_thinking_changed(self, thinking: str) -> None:
        self._bridge.set_worker_thinking(thinking)  # type: ignore[arg-type]
        self._refresh_status_bar()

    def _on_send(self, payload: SendPayload) -> None:
        # Intercept /undo command
        if payload.text.strip().lower() == "/undo":
            self._chat.add_user("/undo")
            self._on_undo()
            return

        if self._bridge.is_running():
            self._message_queue.append(payload)
            self._input.set_queued_messages(len(self._message_queue))
            return
        # Prepare history append: image attachments go via multimodal content array.
        text = payload.text
        # Add text refs from non-image attachments to the text body so the model knows.
        text_refs = [a.text_ref for a in payload.attachments if a.text_ref]
        if text_refs:
            ref_block = "\n".join(text_refs)
            text = f"{text}\n\n{ref_block}".strip() if text else ref_block
        image_atts = [a for a in payload.attachments if a.kind == "image" and a.b64]

        # --- Vision preprocessing ---
        vision_descriptions: list[str] = []
        vision_error: str | None = None
        if image_atts and self._settings.vision_enabled:
            # Show "analyzing" state in input panel
            self._input.set_placeholder("Analyzing images...")
            self._input.setEnabled(False)
            
            def _run_vision():
                nonlocal vision_error
                try:
                    from aura.vision import VisionClient
                    client = VisionClient(
                        endpoint=self._settings.vision_endpoint,
                        model=self._settings.vision_model,
                    )
                    for a in image_atts:
                        desc = client.describe(a.b64)
                        vision_descriptions.append(desc)
                except Exception as exc:
                    vision_error = (
                        f"Local vision model unavailable "
                        f"({self._settings.vision_model}): {exc}"
                    )
                
                # Marshal back to GUI thread to actually send the message
                from PySide6.QtCore import QMetaObject, Qt
                QMetaObject.invokeMethod(self, "_on_vision_done", Qt.QueuedConnection, 
                                        payload, vision_descriptions, vision_error)

            import threading
            threading.Thread(target=_run_vision, daemon=True).start()
            return  # Wait for _on_vision_done

        self._finalize_send(payload, vision_descriptions, vision_error)

    def _on_vision_done(self, payload: SendPayload, descriptions: list[str], error: str | None) -> None:
        self._input.setEnabled(True)
        self._input.set_placeholder("")
        self._finalize_send(payload, descriptions, error)

    def _finalize_send(self, payload: SendPayload, vision_descriptions: list[str], vision_error: str | None) -> None:
        image_atts = [a for a in payload.attachments if a.kind == "image" and a.b64]
        text = payload.text
        text_refs = [a.text_ref for a in payload.attachments if a.text_ref]
        if text_refs:
            ref_block = "\n".join(text_refs)
            text = f"{text}\n\n{ref_block}".strip() if text else ref_block

        if vision_descriptions:
            # Build vision block
            vision_block_parts = []
            for i, desc in enumerate(vision_descriptions):
                vision_block_parts.append(
                    f"[Image {i + 1} description via local vision model:]\n{desc}"
                )
            vision_block = "\n\n---\n\n".join(vision_block_parts)

            if vision_error:
                vision_block += f"\n\n[Vision error: {vision_error}]"

            # Final text for the model
            if text:
                final_text = f"{vision_block}\n\n[User's question:]\n{text}"
            else:
                final_text = vision_block

            # Display text for the UserCard (same as final_text)
            display_text = final_text
        elif vision_error and not vision_descriptions:
            # Vision completely failed — fall back to sending text-only with error note
            final_text = (
                f"{text}\n\n[Note: {vision_error}]"
                if text
                else f"[Vision error: {vision_error}]"
            )
            display_text = final_text
        else:
            # No images or vision disabled
            final_text = text
            display_text = text

        # If vision processed images, ALWAYS use plain text (not multimodal)
        # If vision is disabled and images exist, keep current behavior (multimodal → 400 error)
        if image_atts and not self._settings.vision_enabled:
            # Current behavior: send multimodal (will 400, but user may want to see error)
            parts = []
            if final_text:
                parts.append({"type": "text", "text": final_text})
            for a in image_atts:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{a.b64}"},
                    }
                )
            self._bridge.history.append_user_multimodal(parts)
        else:
            self._bridge.history.append_user_text(final_text)

        self._chat.add_user(display_text, [a.b64 for a in image_atts] or None)
        self._chat.begin_assistant()

        self._bridge.send(
            model=self.current_model(),
            thinking=self.current_thinking(),
        )

    def _on_stop(self) -> None:
        self._bridge.request_cancel()
        self._message_queue.clear()
        self._input.set_queued_messages(0)

    def _on_started(self) -> None:
        self._input.set_streaming(True)

    def _on_finished(self) -> None:
        self._input.set_streaming(False)
        self._chat.assistant_done()
        self._chat.stop_current_aura()
        self._input.focus_editor()
        self._process_message_queue()

    def _process_message_queue(self) -> None:
        """Send the next queued message, if any."""
        if not self._message_queue:
            return
        payload = self._message_queue.pop(0)
        self._input.set_queued_messages(len(self._message_queue))
        self._on_send(payload)

    def _on_stream_done(self, finish_reason: str, full_message: dict) -> None:
        # If the model produced tool calls, it's not actually done — the bridge
        # will execute them and loop back. Keep the aura alive.
        tool_calls = full_message.get("tool_calls") or []
        if tool_calls:
            # Finalize markdown but keep the aura pulsing.
            self._chat.finalize_markdown_only()
            # If any call is a dispatch, transition to "coding" (cyan)
            has_dispatch = any(
                tc.get("function", {}).get("name") == "dispatch_to_worker"
                for tc in tool_calls
            )
            if has_dispatch:
                self._chat.hold_aura_coding()
            # Note: For non-dispatch tool calls, we keep the current aura state
            # (which is usually already "coding" if a tool call was emitted).
        else:
            # No tool calls — this is the final turn.
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
        dlg = SpecApprovalDialog(goal, list(files), spec, acceptance, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._bridge.user_dispatched(
                tool_call_id, dlg.goal(), dlg.files(), dlg.spec(), dlg.acceptance()
            )
        else:
            self._bridge.user_cancelled_dispatch(tool_call_id)

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

    def _on_worker_started(self, tool_call_id: str) -> None:
        # Baton pass: stop the planner's aura so the worker's playground
        # takes over the visual pulse. The planner aura was held alive by
        # finalize_markdown_only() + hold_aura_coding() in _on_stream_done.
        self._chat.stop_current_aura()
        self._playground.begin_assistant()
        self._input.set_streaming(False)

    def _on_worker_finished(self, tool_call_id: str, ok: bool, summary: str) -> None:
        self._playground.worker_finished(ok, summary)

    def _on_worker_cancelled(self, tool_call_id: str) -> None:
        self._playground.worker_cancelled()

    def _on_worker_reasoning(self, tool_call_id: str, text: str) -> None:
        self._playground.append_reasoning(text)

    def _on_worker_content(self, tool_call_id: str, text: str) -> None:
        self._playground.append_content(text)

    def _on_worker_tool_call_start(self, tool_call_id: str, worker_tool_id: str, name: str) -> None:
        self._playground.add_tool_call(worker_tool_id, name)

    def _on_worker_tool_args(self, tool_call_id: str, worker_tool_id: str, fragment: str) -> None:
        self._playground.append_tool_args(worker_tool_id, fragment)

    def _on_worker_tool_result(
        self,
        parent_tool_id: str,
        worker_tool_id: str,
        name: str,
        ok: bool,
        result: str,
        extras: dict,
    ) -> None:
        self._playground.set_tool_result(worker_tool_id, ok, result)

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
        self._playground.add_diff_card(worker_tool_id, rel_path, old, new, decision, is_new_file)

    def _on_worker_api_error(self, tool_call_id: str, status: int, message: str) -> None:
        title = f"API Error {status}" if status > 0 else "Worker Error"
        self._playground.add_error(f"{title}: {message}")

    def _on_view_worker_clicked(self, tool_call_id: str) -> None:
        pass

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

    def _on_worker_todo_list_updated(self, tool_call_id: str, tasks: list) -> None:
        """Route the worker's TODO list update to the Playground's pinned widget."""
        self._playground.update_todo_list(tasks)

    def _on_terminal_output(self, tool_call_id: str, text: str) -> None:
        """Route terminal output (single mode) to the ChatView's TerminalCard."""
        self._chat.append_terminal_output(tool_call_id, text)

    def _on_worker_terminal_output(self, parent_tool_id: str, worker_tool_id: str, text: str) -> None:
        """Route terminal output (worker mode) to the Playground's TerminalCard."""
        self._playground.append_terminal_output(worker_tool_id, text)

    def _on_undo(self) -> None:
        """Handle /undo command — git reset the last commit."""
        if self._workspace_root is None:
            self._chat.add_error("Undo", "No workspace root set.")
            return

        from aura.git import undo_last_commit

        ok, message = undo_last_commit(self._workspace_root)

        if ok:
            # Show a brief assistant response
            ac = self._chat.begin_assistant()
            ac.append_content(f"✅ {message}")
            self._chat.assistant_done()
        else:
            self._chat.add_error("Undo failed", message)

    def _on_api_error(self, status: int, message: str) -> None:
        title = f"API Error {status}" if status > 0 else "Error"
        self._chat.add_error(title, message, show_retry=True)
        self._chat.stop_current_aura()

    def _on_retry(self) -> None:
        if self._bridge.is_running():
            return
        self._chat.begin_assistant()
        self._bridge.send(
            model=self.current_model(),
            thinking=self.current_thinking(),
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

        # Deep copy data for thread safety
        import copy
        history_copy = copy.deepcopy(self._bridge.history)
        dispatch_records_copy = list(self._bridge.dispatch_records)
        workspace_root = self._workspace_root
        model = self.current_model()
        thinking = self.current_thinking()
        worker_model = self.current_worker_model()
        worker_thinking = self.current_worker_thinking()
        existing_path = self._current_conversation_path
        pwm = self._bridge.planner_worker_mode
        provider = self._settings.provider

        def _run_save():
            try:
                path = save_conversation(
                    history=history_copy,
                    workspace_root=workspace_root,
                    model=model,
                    thinking=thinking,
                    existing_path=existing_path,
                    planner_worker_mode=pwm,
                    planner_model=model,
                    worker_model=worker_model,
                    planner_thinking=thinking,
                    worker_thinking=worker_thinking,
                    worker_dispatches=dispatch_records_copy,
                    provider=provider,
                )
                # Update the current path pointer on the GUI thread
                from PySide6.QtCore import QMetaObject, Qt
                QMetaObject.invokeMethod(self, "_set_current_conv_path", Qt.QueuedConnection, path)
            except OSError as exc:
                from PySide6.QtCore import QMetaObject, Qt
                QMetaObject.invokeMethod(self._chat, "add_error", Qt.QueuedConnection, 
                                        "Could not save conversation", str(exc))

        import threading
        threading.Thread(target=_run_save, daemon=True).start()

    @Slot(Path)
    def _set_current_conv_path(self, path: Path) -> None:
        self._current_conversation_path = path

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

        # Propagate custom prompts to bridge for future mode switches
        self._bridge.set_custom_system_prompts(
            self._settings.system_prompt,
            self._settings.planner_system_prompt,
            self._settings.worker_system_prompt,
        )
        self._bridge.set_temperature(self._settings.temperature)
        self._bridge.set_worker_temperature(self._settings.worker_temperature)

        # If the loaded conversation has a different provider, update the bridge.
        if loaded.provider != self._settings.provider:
            self._settings.provider = loaded.provider
            self._bridge.set_provider(loaded.provider)
            self._populate_model_combos(loaded.provider)

        # Sync mode (without overwriting the system prompt we just set).
        self._bridge.set_planner_worker_mode(pwm)
        if pwm:
            self.set_model(loaded.planner_model)
            self.set_thinking(loaded.planner_thinking)
            self.set_worker_model(loaded.worker_model)
            self.set_worker_thinking(loaded.worker_thinking)
            self._bridge.set_worker_model(loaded.worker_model)
            self._bridge.set_worker_thinking(loaded.worker_thinking)
        else:
            self.set_model(loaded.model)
            self.set_thinking(loaded.thinking)
        self._set_sidebar_planner_worker_mode(pwm)
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
