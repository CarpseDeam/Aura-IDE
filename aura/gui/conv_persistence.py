"""Conversation persistence — save, load, restore, replay lifecycle.

Owns all conversation save/load/restore/replay logic that was previously
in MainWindow. Emits signals so the UI layer can react.
"""
from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

from aura.config import APP_NAME
from aura.conversation.chat_transcript import (
    ASSISTANT,
    ERROR,
    EXECUTION_COMPLETE,
    PLAN_REVIEW,
    USER,
    clone_chat_items,
    legacy_chat_items_from_messages,
)
from aura.conversation.history import History
from aura.conversation.persistence import (
    LoadedConversation,
    _first_user_text,
    load_conversation,
    most_recent_conversation,
    save_conversation,
)
from aura.conversation.telemetry import ConversationTelemetry
from aura.projects.store import ProjectStore
from aura.settings import AppSettings


def _is_transient_replay_message(m: dict) -> bool:
    """Return True if *m* is a runtime artifact that should not appear in restored chat.

    Skips:
    - Synthetic stale-read invalidation user notices.
    - Assistant messages whose purpose was tool dispatch/progress.
    """
    role = m.get("role")
    if m.get("aura_internal"):
        return True
    if role == "user":
        content = m.get("content", "")
        if isinstance(content, str) and content.startswith("Production stale-read invalidation:"):
            return True
    elif role == "assistant":
        # Assistant messages with tool_calls are operational chatter, not final prose.
        if m.get("tool_calls"):
            return True
    return False


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
    # Emitted when the active project or conversation context changes.
    current_context_changed = Signal(str, str)  # (project_id, thread_id)

    def __init__(
        self,
        bridge,
        chat,
        playground,
        input_panel,
        left_pane,
        settings,
        get_conversation_telemetry: Callable[[], ConversationTelemetry],
        restore_conversation_telemetry: Callable[[ConversationTelemetry], None],
        reset_conversation_usage: Callable[[], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._chat = chat
        self._playground = playground
        self._input = input_panel
        self._left_pane = left_pane
        self._settings = settings
        self._get_conversation_telemetry = get_conversation_telemetry
        self._restore_conversation_telemetry = restore_conversation_telemetry
        self._reset_conversation_usage = reset_conversation_usage
        self._current_conversation_path: Path | None = None
        self._active_replay_id: int = 0
        self._conversation_generation: int = 0

        self.save_succeeded.connect(self._on_save_succeeded)
        # A save failure is a notice about this session, not conversation
        # content — never record it into the transcript we are trying to save.
        self.save_failed.connect(
            lambda msg: self._chat.add_error(
                "Could not save conversation", msg, persist=False
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
            self.current_context_changed.emit(project.id, thread.id)
        except Exception:
            logging.exception("Failed to update project thread metadata")

    def auto_save(
        self,
        workspace_root,
        model,
        thinking,
        provider,
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
        chat_items_copy = clone_chat_items(getattr(self._chat, "chat_items", []))
        telemetry_copy = ConversationTelemetry.from_dict(
            self._get_conversation_telemetry().to_dict()
        )
        existing_path = self._current_conversation_path

        # Guard: if existing_path is not under the current workspace root's
        # conversations dir, ignore it to prevent cross-project contamination.
        if existing_path is not None:
            from aura.conversation.persistence import conversations_dir
            from aura.paths import safe_is_relative_to
            if not safe_is_relative_to(existing_path, conversations_dir(workspace_root)):
                existing_path = None

        def _run_save() -> None:
            try:
                path = save_conversation(
                    history=history_copy,
                    workspace_root=workspace_root,
                    model=model,
                    thinking=thinking,
                    existing_path=existing_path,
                    chat_items=chat_items_copy,
                    provider=provider,
                    telemetry=telemetry_copy,
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
        self._bridge.clear_pre_execution_snapshot()
        self._chat.reset()
        self._playground.clear()
        self._current_conversation_path = None
        self._reset_conversation_usage()
        self.current_context_changed.emit("", "")

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
            return self.load_and_apply(Path(chosen))
        except ValueError:
            QMessageBox.warning(
                parent_widget,
                APP_NAME,
                "That conversation belongs to another workspace.",
            )
            return None
        except Exception as exc:
            QMessageBox.warning(
                parent_widget,
                APP_NAME,
                f"Could not open conversation:\n{exc}",
            )
            return None
        
    def load_and_apply(self, path: Path) -> LoadedConversation:
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
        return loaded

    def restore_last(self, workspace_root) -> None:
        """Restore the most recently saved conversation, if any.

        Silently returns if there is no saved conversation or loading fails.
        """
        if workspace_root is None:
            return
        # Guard: no-op if the active workspace has changed since this was scheduled
        bridge_ws = self._bridge.registry.workspace_root
        if bridge_ws is not None:
            from aura.paths import safe_is_relative_to
            if not safe_is_relative_to(workspace_root, bridge_ws):
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
        logging.getLogger(__name__).info(
            "DIAGNOSTIC ConversationPersistence.apply_loaded — full view reset + replay path=%s",
            loaded.path,
        )
        self._active_replay_id += 1
        self._bridge.reset_history()
        # Old conversations may carry legacy Assistant/Execution metadata. They load
        # safely, but the live session always resumes with the production
        # conversation's model fields only.
        from aura.prompts import PRODUCTION_SYSTEM_PROMPT
        default_prompt = PRODUCTION_SYSTEM_PROMPT

        self._bridge.history.system_prompt = (
            loaded.history.system_prompt or default_prompt
        )
        self._bridge.history.messages = list(loaded.history.messages)
        self._current_conversation_path = loaded.path

        # Propagate the custom production prompt.
        self._bridge.set_system_prompt(self._settings.system_prompt)
        self._bridge.set_temperature(self._settings.temperature)

        # Update settings to match the loaded conversation.
        self._settings.provider = loaded.provider

        # Restore the production provider to bridge and sidebar.
        self._bridge.set_production_provider(loaded.provider)
        self._left_pane.populate_models(loaded.provider)

        # Always resume the production conversation.
        self._bridge.refresh_production_prompt()
        if loaded.model:
            self._left_pane.set_production_model(loaded.model)
        if loaded.thinking:
            self._left_pane.set_production_thinking(loaded.thinking)
        self._chat.reset()
        self._playground.clear()
        self._bridge.clear_pre_execution_snapshot()
        self._render_chat_items(loaded.chat_items)
        self._restore_conversation_telemetry(loaded.telemetry)
        self.needs_status_refresh.emit()

        # Sync companion context after applying a loaded conversation
        try:
            ws = self._bridge.registry.workspace_root
            if ws and loaded.path:
                store = ProjectStore()
                project = store.create_or_update_project(ws)
                found = False
                for t in store.list_threads(project, include_archived=True):
                    if t.conversation_path == loaded.path:
                        self.current_context_changed.emit(project.id, t.id)
                        found = True
                        break
                if not found:
                    # Thread not found — still emit project (with empty thread)
                    self.current_context_changed.emit(project.id, "")
        except Exception:
            logging.exception("Failed to sync companion context after loading conversation")

    # ---- replay history into view ------------------------------------------

    def replay_history(self, *, synchronous: bool = False) -> None:
        """Replay a safe legacy transcript from current model history.

        Used by retry/rerun flows after model history has been rewound. Normal
        conversation load renders persisted ``chat_items`` instead.
        """
        msgs = self._bridge.history.messages
        if not msgs:
            return

        # Cancel any in-flight replay
        self._active_replay_id += 1
        my_id = self._active_replay_id

        process_items = legacy_chat_items_from_messages([
            m for m in msgs
            if not _is_transient_replay_message(m)
        ])
        msg_iter = iter(process_items)
        self._chat.begin_bulk_update()
        if hasattr(self._chat, "begin_transcript_replay"):
            self._chat.begin_transcript_replay()

        def process_chunk() -> None:
            if self._active_replay_id != my_id:
                return

            chunk_size = max(1, len(process_items)) if synchronous else 10
            try:
                for _ in range(chunk_size):
                    item = next(msg_iter)
                    kind = item.get("kind")
                    if kind == USER:
                        self._chat.add_user(str(item.get("text", "")))
                    elif kind == ASSISTANT:
                        self._chat.begin_assistant()
                        self._chat.append_content(str(item.get("text", "")))
                        self._chat.assistant_done()

                # Schedule next chunk
                if synchronous:
                    process_chunk()
                else:
                    QTimer.singleShot(0, process_chunk)
            except StopIteration:
                if self._active_replay_id == my_id:
                    if hasattr(self._chat, "end_transcript_replay"):
                        self._chat.end_transcript_replay(process_items)
                    self._chat.end_bulk_update()

        if synchronous:
            process_chunk()
        else:
            # Defer the first chunk as well to keep the UI thread moving.
            QTimer.singleShot(0, process_chunk)

    def _render_chat_items(self, items: list[dict]) -> None:
        """Render durable chat transcript items without runtime reconstruction."""
        render_items = clone_chat_items(items)
        self._active_replay_id += 1
        self._chat.begin_bulk_update()
        if hasattr(self._chat, "begin_transcript_replay"):
            self._chat.begin_transcript_replay()
        for item in render_items:
            kind = item.get("kind")
            if kind == USER:
                image_b64s = item.get("image_b64s")
                self._chat.add_user(
                    str(item.get("text", "")),
                    image_b64s if isinstance(image_b64s, list) else None,
                )
            elif kind == ASSISTANT:
                self._chat.begin_assistant()
                self._chat.append_content(str(item.get("text", "")))
                self._chat.assistant_done()
            elif kind == ERROR:
                self._chat.add_error(
                    str(item.get("title", "")),
                    str(item.get("message", "")),
                    bool(item.get("show_retry", False)),
                )
            elif kind == EXECUTION_COMPLETE:
                # Execution completion records are durable diagnostics only.
                # Replaying them must not recreate main-chat finish cards.
                continue
            elif kind == PLAN_REVIEW:
                # Replay a resolved Plan Review in its non-interactive final
                # state: never a pending review, never re-executed.
                self._chat.begin_assistant()
                raw_files = item.get("files")
                files = [str(f) for f in raw_files] if isinstance(raw_files, list) else []
                card = self._chat.add_plan_review_card(
                    "",
                    str(item.get("goal", "")),
                    files,
                    str(item.get("spec", "")),
                    str(item.get("acceptance", "")),
                    str(item.get("summary", "")),
                )
                card.show_resolved(approved=bool(item.get("approved", False)))
                self._chat.assistant_done()
        if hasattr(self._chat, "end_transcript_replay"):
            self._chat.end_transcript_replay(render_items)
        self._chat.end_bulk_update()
