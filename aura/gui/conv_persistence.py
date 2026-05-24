"""Conversation persistence — save, load, restore, replay lifecycle.

Owns all conversation save/load/restore/replay logic that was previously
in MainWindow. Emits signals so the UI layer can react.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from aura.config import APP_NAME
from aura.settings import AppSettings
from aura.conversation.history import History
from aura.conversation.persistence import (
    LoadedConversation,
    _first_user_text,
    load_conversation,
    most_recent_conversation,
    save_conversation,
)
from aura.projects.store import ProjectStore


class ConversationPersistence(QObject):
    """Owns the save/load/restore/replay lifecycle for conversations.

    Encapsulates all disk I/O and history-replay logic so that MainWindow
    only delegates to this class via simple method calls.
    """

    # Emitted when a conversation was saved successfully (with the file path
    # and conversation generation active when the save started).
    save_succeeded = Signal(Path, int)
    # Emitted when saving failed (with the error message).
    save_failed = Signal(str)
    # Emitted after apply_loaded finishes so the UI can refresh status.
    needs_status_refresh = Signal()
    # Emitted after project thread metadata is updated by auto-save.
    project_thread_updated = Signal()

    def __init__(
        self,
        bridge,
        chat,
        playground,
        input_panel,
        left_pane,
        settings,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._chat = chat
        self._playground = playground
        self._input = input_panel
        self._left_pane = left_pane
        self._settings = settings
        self._current_conversation_path: Path | None = None
        self._active_replay_id: int = 0
        self._conversation_generation: int = 0

        self.save_succeeded.connect(self._on_save_succeeded)
        self.save_failed.connect(
            lambda msg: self._chat.add_error(
                "Could not save conversation", msg
            )
        )

    # ---- public property ---------------------------------------------------

    @property
    def current_conversation_path(self) -> Path | None:
        """The file path of the most recently saved/loaded conversation."""
        return self._current_conversation_path

    def update_settings(self, settings: AppSettings) -> None:
        """Use the latest settings object for future restore/replay operations."""
        self._settings = settings

    # ---- internal slots ----------------------------------------------------

    @Slot(Path, int)
    def _on_save_succeeded(self, path: Path, generation: int) -> None:
        if generation != self._conversation_generation:
            return
        self._current_conversation_path = path

    # ---- auto-save ---------------------------------------------------------

    def _update_project_thread(
        self, workspace_root: Path, conversation_path: Path, history: History
    ) -> None:
        """Ensure workspace has a ProjectSpace and conversation has a thread.

        Creates `.aura/project.json` if missing. Looks up an existing thread
        by matching `conversation_path` against all threads in the project.
        Creates a new thread on first save. Updates thread metadata and
        `ProjectSpace.last_thread_id` on every save.  Silently catches all
        exceptions so thread-metadata failures never break the conversation save.
        """
        try:
            store = ProjectStore()
            project = store.create_or_update_project(workspace_root)

            # Find existing thread for this conversation path
            thread = None
            for t in store.list_threads(project, include_archived=True):
                if t.conversation_path == conversation_path:
                    thread = t
                    break

            if thread is None:
                raw_title = _first_user_text(history) or "Conversation"
                clean_fn = getattr(ProjectStore, "clean_thread_title")
                title = clean_fn(raw_title)
                thread = store.create_thread(project, title=title)

            thread.conversation_path = conversation_path
            store.save_thread(project, thread)
            project.last_thread_id = thread.id
            store.save_project(project)
            self.project_thread_updated.emit()
        except Exception:
            logging.exception("Failed to update project thread metadata")

    def auto_save(
        self,
        workspace_root,
        model,
        thinking,
        worker_model,
        worker_thinking,
        provider,
        planner_provider,
        worker_provider,
    ) -> None:
        """Save the current conversation in a background thread (fire-and-forget).

        Guards against missing workspace or empty history.  Deep-copies all
        mutable state before handing it to the save thread.
        """
        if workspace_root is None:
            return
        if not self._bridge.history.messages:
            return

        generation = self._conversation_generation

        # Deep copy data for thread safety
        history_copy = copy.deepcopy(self._bridge.history)
        dispatch_records_copy = list(self._bridge.dispatch_records)
        existing_path = self._current_conversation_path

        # Guard: if existing_path is not under the current workspace root's
        # conversations dir, ignore it to prevent cross-project contamination.
        if existing_path is not None:
            from aura.conversation.persistence import conversations_dir
            from aura.paths import safe_is_relative_to
            if not safe_is_relative_to(existing_path, conversations_dir(workspace_root)):
                existing_path = None

        pwm = self._bridge.planner_worker_mode

        def _run_save() -> None:
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
                    planner_provider=planner_provider,
                    worker_provider=worker_provider,
                )
                self._update_project_thread(workspace_root, path, history_copy)
                self.save_succeeded.emit(path, generation)
            except OSError as exc:
                self.save_failed.emit(str(exc))

        threading.Thread(target=_run_save, daemon=True).start()

    # ---- new / open / restore ----------------------------------------------

    def new_conversation(self) -> None:
        """Reset all state for a brand-new conversation."""
        self._active_replay_id += 1
        self._conversation_generation += 1
        self._bridge.reset_history()
        self._bridge.clear_pre_worker_snapshot()
        self._chat.reset()
        self._playground.clear()
        self._current_conversation_path = None

    def open_conversation(
        self, workspace_root, parent_widget
    ) -> LoadedConversation | None:
        """Show a file-open dialog and load a conversation.

        Returns the loaded conversation on success, or *None* if the user
        cancelled or an error occurred.
        """
        if workspace_root is None:
            return None
        start = str(workspace_root / ".aura" / "conversations")
        Path(start).mkdir(parents=True, exist_ok=True)
        from aura.git_ops import ensure_aura_gitignored
        ensure_aura_gitignored(workspace_root)

        chosen, _ = QFileDialog.getOpenFileName(
            parent_widget,
            "Open Conversation",
            start,
            "Conversations (*.json)",
        )
        if not chosen:
            return None
        try:
            loaded = load_conversation(Path(chosen))
        except Exception as exc:
            QMessageBox.warning(
                parent_widget,
                APP_NAME,
                f"Could not open conversation:\n{exc}",
            )
            return None
        self.apply_loaded(loaded)
        return loaded
        
    def load_and_apply(self, path: Path) -> None:
        """Load a conversation from a file path and apply it to the live bridge/view.

        Raises ValueError if the path lies outside the active workspace's
        conversations directory (cross-project guard).
        """
        # Guard: refuse to load a conversation outside the active workspace
        ws = self._bridge.registry.workspace_root
        if ws is not None:
            from aura.conversation.persistence import conversations_dir
            from aura.paths import safe_is_relative_to
            if not safe_is_relative_to(path, conversations_dir(ws)):
                raise ValueError(
                    f"Cannot load conversation from outside the active workspace:\n"
                    f"  Path: {path}\n"
                    f"  Workspace: {ws}"
                )
        loaded = load_conversation(path)
        self.apply_loaded(loaded)

    def restore_last(self, workspace_root) -> None:
        """Restore the most recently saved conversation, if any.

        Silently returns if there is no saved conversation or loading fails.
        """
        if workspace_root is None:
            return
        path = most_recent_conversation(workspace_root)
        if path is None:
            return
        try:
            loaded = load_conversation(path)
        except Exception:
            return
        self.apply_loaded(loaded)

    # ---- apply loaded conversation -----------------------------------------

    def apply_loaded(self, loaded: LoadedConversation) -> None:
        """Apply a loaded conversation to the live bridge / view state.

        Sets history, reconfigures provider/model/thinking, clears the view,
        then replays all messages into the chat.
        """
        self._active_replay_id += 1
        pwm = loaded.planner_worker_mode
        from aura.prompts import PLANNER_SYSTEM_PROMPT, SINGLE_SYSTEM_PROMPT
        default_prompt = PLANNER_SYSTEM_PROMPT if pwm else SINGLE_SYSTEM_PROMPT

        self._bridge.history.system_prompt = (
            loaded.history.system_prompt or default_prompt
        )
        self._bridge.history.messages = list(loaded.history.messages)
        self._current_conversation_path = loaded.path

        # Propagate custom prompts to bridge for future mode switches
        self._bridge.set_custom_system_prompts(
            self._settings.system_prompt,
            self._settings.planner_system_prompt,
            self._settings.worker_system_prompt,
        )
        self._bridge.set_temperature(self._settings.temperature)
        self._bridge.set_worker_temperature(self._settings.worker_temperature)

        # Update settings to match loaded conversation
        self._settings.provider = loaded.provider
        self._settings.planner_provider = loaded.planner_provider
        self._settings.worker_provider = loaded.worker_provider

        # Restore providers to bridge and sidebar
        self._bridge.set_planner_provider(loaded.planner_provider)
        self._bridge.set_worker_provider(loaded.worker_provider)
        self._left_pane.populate_models(loaded.planner_provider, loaded.worker_provider)

        # Sync mode (without overwriting the system prompt we just set).
        self._bridge.set_planner_worker_mode(pwm)
        self._bridge.set_dispatch_records(loaded.worker_dispatches)
        if pwm:
            self._left_pane.set_planner_model(loaded.planner_model)
            self._left_pane.set_planner_thinking(loaded.planner_thinking)
            self._left_pane.set_worker_model(loaded.worker_model)
            self._left_pane.set_worker_thinking(loaded.worker_thinking)
            self._bridge.set_worker_model(loaded.worker_model)
            self._bridge.set_worker_thinking(loaded.worker_thinking)
        else:
            self._left_pane.set_planner_model(loaded.model)
            self._left_pane.set_planner_thinking(loaded.thinking)

        self._left_pane.set_planner_worker_mode(pwm)
        self._chat.reset()
        self._playground.clear()
        self._bridge.clear_pre_worker_snapshot()
        self.replay_history()
        self.needs_status_refresh.emit()

    # ---- replay history into view ------------------------------------------

    def replay_history(self, *, synchronous: bool = False) -> None:
        """Best-effort visual replay of a loaded history into the chat view.

        We intentionally don't try to recreate diff cards (the underlying
        before/after content isn't stored — only the resulting tool-message
        from the registry is).  Tool calls are surfaced as cards with their
        recorded args + result so the conversation reads coherently.
        """
        msgs = self._bridge.history.messages
        if not msgs:
            return

        # Cancel any in-flight replay
        self._active_replay_id += 1
        my_id = self._active_replay_id

        # Index tool results by tool_call_id for inline pairing.
        tool_results: dict[str, str] = {}
        for m in msgs:
            if m.get("role") == "tool":
                tcid = m.get("tool_call_id")
                if isinstance(tcid, str):
                    tool_results[tcid] = m.get("content", "")

        self._chat.begin_bulk_update()

        # Filter out tool messages as they are paired into assistant cards.
        process_msgs = [m for m in msgs if m.get("role") != "tool"]
        msg_iter = iter(process_msgs)

        def process_chunk() -> None:
            if self._active_replay_id != my_id:
                return

            chunk_size = max(1, len(process_msgs)) if synchronous else 10
            try:
                for _ in range(chunk_size):
                    m = next(msg_iter)
                    role = m.get("role")
                    if role == "user":
                        content = m.get("content")
                        if isinstance(content, str):
                            self._chat.add_user(content)
                        elif isinstance(content, list):
                            text_parts = [
                                p.get("text", "")
                                for p in content
                                if isinstance(p, dict)
                                and p.get("type") == "text"
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
                            args_str = fn.get("arguments", "")

                            self._chat.add_tool_call(tcid, name)
                            if args_str:
                                self._chat.append_tool_args(tcid, args_str)

                            if tcid in tool_results:
                                ok = True
                                try:
                                    parsed = json.loads(tool_results[tcid])
                                    if (
                                        isinstance(parsed, dict)
                                        and parsed.get("ok") is False
                                    ):
                                        ok = False
                                except json.JSONDecodeError:
                                    pass
                                self._chat.set_tool_result(
                                    tcid, ok, tool_results[tcid]
                                )
                        self._chat.assistant_done()

                # Schedule next chunk
                if synchronous:
                    process_chunk()
                else:
                    QTimer.singleShot(0, process_chunk)
            except StopIteration:
                if self._active_replay_id == my_id:
                    self._chat.end_bulk_update()

        if synchronous:
            process_chunk()
        else:
            # Defer the first chunk as well to keep the UI thread moving.
            QTimer.singleShot(0, process_chunk)
