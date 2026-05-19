"""Edit animation state machine for progressive reveal and transition effects."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

from aura.gui.editor.diff_overlay import DiffOverlay
from aura.gui.smooth_code_streamer import SmoothCodeStreamer


class EditAnimation:
    """Class-level methods for animating code edits in a QPlainTextEdit.

    Call as ``EditAnimation.tick(state, editor, set_status=...)``.
    """

    ANIM_TICK_MS = 16
    TYPE_CHARS_PER_TICK = 36
    DELETE_CHARS_PER_TICK = 32
    DELETE_LINES_PER_TICK = 2
    RETYPE_CHARS_PER_TICK = 20
    DELETE_HOLD_TICKS = 7
    TYPE_HOLD_TICKS = 3
    INSTANT_TOTAL_CHARS = 200_000
    INSTANT_CHANGED_CHARS = 20_000

    # ------------------------------------------------------------------
    # Static utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def compute_changed_region(
        old_text: str, new_text: str
    ) -> tuple[int, int, str, str]:
        prefix_len = 0
        while (
            prefix_len < len(old_text)
            and prefix_len < len(new_text)
            and old_text[prefix_len] == new_text[prefix_len]
        ):
            prefix_len += 1

        suffix_len = 0
        while (
            suffix_len < len(old_text) - prefix_len
            and suffix_len < len(new_text) - prefix_len
            and old_text[len(old_text) - 1 - suffix_len]
            == new_text[len(new_text) - 1 - suffix_len]
        ):
            suffix_len += 1

        old_middle = (
            old_text[prefix_len:len(old_text) - suffix_len]
            if suffix_len
            else old_text[prefix_len:]
        )
        new_middle = (
            new_text[prefix_len:len(new_text) - suffix_len]
            if suffix_len
            else new_text[prefix_len:]
        )
        return prefix_len, suffix_len, old_middle, new_middle

    @staticmethod
    def line_start(text: str, position: int) -> int:
        position = max(0, min(position, len(text)))
        return text.rfind("\n", 0, position) + 1

    @staticmethod
    def line_end(text: str, position: int) -> int:
        position = max(0, min(position, len(text)))
        newline = text.find("\n", position)
        return len(text) if newline == -1 else newline + 1

    @staticmethod
    def line_number_at(text: str, position: int) -> int:
        position = max(0, min(position, len(text)))
        return text.count("\n", 0, position) + 1

    @classmethod
    def compute_animation_region(
        cls, old_text: str, new_text: str
    ) -> tuple[int, int, int, int]:
        """Return old/new character ranges to animate.

        Multiline replacements are expanded to whole logical lines so the UI
        can show \"delete these lines, type these lines\" instead of a noisy
        middle-of-buffer character churn. Pure insertions keep an empty old
        range so existing lines do not flash or disappear.
        """
        prefix_len, suffix_len, old_mid, new_mid = cls.compute_changed_region(
            old_text, new_text
        )
        old_raw_end = len(old_text) - suffix_len if suffix_len else len(old_text)
        new_raw_end = len(new_text) - suffix_len if suffix_len else len(new_text)

        pure_insert = not old_mid and bool(new_mid)
        pure_delete = bool(old_mid) and not new_mid
        multiline_change = "\n" in old_mid or "\n" in new_mid
        replacement = bool(old_mid and new_mid)

        if pure_insert and "\n" in new_mid:
            old_start = cls.line_start(old_text, prefix_len)
            new_start = cls.line_start(new_text, prefix_len)
            new_end = (
                cls.line_start(new_text, new_raw_end)
                if suffix_len
                else len(new_text)
            )
            return old_start, old_start, new_start, new_end

        if pure_insert:
            return prefix_len, prefix_len, prefix_len, new_raw_end

        if pure_delete and "\n" in old_mid:
            old_start = cls.line_start(old_text, prefix_len)
            old_end = (
                cls.line_start(old_text, old_raw_end)
                if suffix_len
                else len(old_text)
            )
            new_start = cls.line_start(new_text, prefix_len)
            return old_start, old_end, new_start, new_start

        if multiline_change or replacement:
            old_start = cls.line_start(old_text, prefix_len)
            old_end = cls.line_end(old_text, old_raw_end)
            new_start = cls.line_start(new_text, prefix_len)
            new_end = cls.line_end(new_text, new_raw_end)
            return old_start, old_end, new_start, new_end

        return prefix_len, old_raw_end, prefix_len, new_raw_end

    @classmethod
    def should_animate(cls, old_text: str, new_text: str) -> bool:
        if (
            len(old_text) > cls.INSTANT_TOTAL_CHARS
            or len(new_text) > cls.INSTANT_TOTAL_CHARS
        ):
            return False
        _prefix_len, _suffix_len, old_mid, new_mid = cls.compute_changed_region(
            old_text, new_text
        )
        return max(len(old_mid), len(new_mid)) <= cls.INSTANT_CHANGED_CHARS

    @staticmethod
    def set_editor_text(
        editor: QPlainTextEdit, text: str, cursor_position: int | None = None
    ) -> None:
        editor.setPlainText(text)
        if cursor_position is None:
            return
        cursor = editor.textCursor()
        cursor.setPosition(max(0, min(cursor_position, len(text))))
        editor.setTextCursor(cursor)
        editor.ensureCursorVisible()

    @staticmethod
    def focus_editor_position(editor: QPlainTextEdit, position: int) -> None:
        cursor = QTextCursor(editor.document())
        cursor.setPosition(max(0, min(position, len(editor.toPlainText()))))
        editor.setTextCursor(cursor)
        editor.centerCursor()

    # ------------------------------------------------------------------
    # Animation tick dispatch
    # ------------------------------------------------------------------

    @classmethod
    def tick(
        cls,
        state: dict,
        editor: QPlainTextEdit,
        set_status: Callable[[str, str], None] | None = None,
    ) -> None:
        """One frame of animation. Dispatches to phase handler."""
        phase = state.get("animation_phase") or "type"
        if phase == "delete_hold":
            cls.tick_hold_phase(state, editor, "delete", set_status)
            return
        if phase == "replace_hold":
            cls.tick_hold_phase(state, editor, "replace", set_status)
            return
        if phase == "replace":
            cls.start_replacement_phase(state, editor, set_status)
            return
        if phase == "delete":
            cls.tick_delete_phase(state, editor, set_status)
            return
        if phase == "type_hold":
            cls.tick_hold_phase(state, editor, "retype", set_status)
            return
        if phase == "retype":
            cls.tick_retype_phase(state, editor, set_status)
            return

        target = state["target"]
        state["timer"].stop()
        state["animation_phase"] = ""
        state["streamer"].set_target(target)

    # ------------------------------------------------------------------
    # Phase handlers
    # ------------------------------------------------------------------

    @classmethod
    def tick_hold_phase(
        cls,
        state: dict,
        editor: QPlainTextEdit,
        next_phase: str,
        set_status: Callable[[str, str], None] | None = None,
    ) -> None:
        state["animation_hold_ticks"] = max(
            0, state.get("animation_hold_ticks", 0) - 1
        )
        if state["animation_hold_ticks"] > 0:
            return
        state["animation_phase"] = next_phase
        if next_phase == "retype":
            prefix = state["animation_prefix"]
            suffix = state["animation_suffix"]
            cls.set_editor_text(editor, prefix + suffix, len(prefix))
            cls.focus_editor_position(editor, len(prefix))
            DiffOverlay.clear(editor)
        elif next_phase == "replace":
            cls.start_replacement_phase(state, editor, set_status)

    @classmethod
    def start_replacement_phase(
        cls,
        state: dict,
        editor: QPlainTextEdit,
        set_status: Callable[[str, str], None] | None = None,
    ) -> None:
        state["timer"].stop()
        state["animation_phase"] = "replace"
        prefix = state["animation_prefix"]
        suffix = state["animation_suffix"]
        new_mid = state["animation_new_middle"]
        old_start = state.get("animation_old_start", len(prefix))
        old_end = state.get("animation_old_end", old_start)
        cursor = QTextCursor(editor.document())
        text_len = max(0, editor.document().characterCount() - 1)
        cursor.setPosition(max(0, min(old_start, text_len)))
        cursor.setPosition(
            max(0, min(old_end, text_len)),
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.insertText("")
        DiffOverlay.clear(editor)
        if set_status is not None:
            set_status(
                state.get("tool_id", ""),
                f":{state['animation_change_line']} - typing",
            )
        streamer: SmoothCodeStreamer = state["streamer"]
        streamer.start_replacement(
            prefix,
            new_mid,
            suffix,
            base_already_set=True,
        )

    @classmethod
    def tick_delete_phase(
        cls,
        state: dict,
        editor: QPlainTextEdit,
        set_status: Callable[[str, str], None] | None = None,
    ) -> None:
        prefix = state["animation_prefix"]
        suffix = state["animation_suffix"]
        old_lines = state.get("animation_old_lines") or []

        if len(old_lines) > 1:
            state["animation_delete_line_count"] = max(
                0,
                state["animation_delete_line_count"] - cls.DELETE_LINES_PER_TICK,
            )
            remaining = "".join(old_lines[:state["animation_delete_line_count"]])
            display = prefix + remaining + suffix
            cursor_pos = len(prefix) + len(remaining)
            cls.set_editor_text(editor, display, cursor_pos)
            if remaining:
                DiffOverlay.mark_deleted(editor, len(prefix), cursor_pos)
            else:
                DiffOverlay.clear(editor)
        else:
            state["animation_char_index"] = max(
                0,
                len(state["animation_old_middle"])
                - cls.DELETE_CHARS_PER_TICK
                if state["animation_char_index"] == 0
                else state["animation_char_index"] - cls.DELETE_CHARS_PER_TICK,
            )
            remaining = state["animation_old_middle"][:state["animation_char_index"]]
            display = prefix + remaining + suffix
            cls.set_editor_text(editor, display, len(prefix) + len(remaining))
            if remaining:
                DiffOverlay.mark_deleted(
                    editor, len(prefix), len(prefix) + len(remaining)
                )
            else:
                DiffOverlay.clear(editor)

        no_lines_left = state.get("animation_delete_line_count", 0) == 0
        no_chars_left = (
            len(old_lines) <= 1 and state.get("animation_char_index", 0) == 0
        )
        if no_lines_left or no_chars_left:
            if state["animation_new_middle"]:
                state["animation_phase"] = "type_hold"
                state["animation_hold_ticks"] = cls.TYPE_HOLD_TICKS
                state["animation_char_index"] = 0
                if set_status is not None:
                    set_status(
                        state.get("tool_id", ""),
                        f":{state['animation_change_line']} - typing",
                    )
            else:
                cls.finish_edit_animation(state, editor, set_status)

    @classmethod
    def tick_retype_phase(
        cls,
        state: dict,
        editor: QPlainTextEdit,
        set_status: Callable[[str, str], None] | None = None,
    ) -> None:
        new_mid = state["animation_new_middle"]
        state["animation_char_index"] = min(
            len(new_mid),
            state["animation_char_index"] + cls.RETYPE_CHARS_PER_TICK,
        )
        prefix = state["animation_prefix"]
        suffix = state["animation_suffix"]
        idx = state["animation_char_index"]
        display = prefix + new_mid[:idx] + suffix
        cls.set_editor_text(editor, display, len(prefix) + idx)
        DiffOverlay.mark_inserted(editor, len(prefix), len(prefix) + idx)
        if idx >= len(new_mid):
            cls.finish_edit_animation(state, editor, set_status)

    @classmethod
    def finish_edit_animation(
        cls,
        state: dict,
        editor: QPlainTextEdit,
        set_status: Callable[[str, str], None] | None = None,
    ) -> None:
        state["timer"].stop()
        final_text = (
            state["animation_prefix"]
            + state["animation_new_middle"]
            + state["animation_suffix"]
        )
        state["target"] = final_text
        state["position"] = len(final_text)
        state["animation_phase"] = ""
        cls.set_editor_text(editor, final_text, len(state["animation_prefix"]))
        change_start = state.get(
            "animation_change_start", len(state["animation_prefix"])
        )
        change_end = change_start + len(state.get("animation_new_middle", ""))
        if change_end > change_start:
            DiffOverlay.mark_inserted(editor, change_start, change_end)
        else:
            DiffOverlay.clear(editor)
        if set_status is not None:
            set_status(state.get("tool_id", ""), " ✓")

    @classmethod
    def on_streamer_finished(
        cls,
        state: dict,
        editor: QPlainTextEdit,
        set_status: Callable[[str, str], None] | None = None,
    ) -> None:
        state["position"] = len(state["streamer"].visible_text())
        state["animation_phase"] = ""
        if state.get("animation_new_middle"):
            change_start = state.get("animation_change_start", 0)
            change_end = change_start + len(state.get("animation_new_middle", ""))
            if change_end > change_start:
                DiffOverlay.mark_inserted(editor, change_start, change_end)
        if state.get("pending_done") and state.get("active_count", 0) == 0:
            state["pending_done"] = False
            if set_status is not None:
                set_status(state.get("tool_id", ""), " ✓")
