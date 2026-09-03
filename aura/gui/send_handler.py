"""Handles send/stop/undo logic extracted from MainWindow.

Owns the message queue and undo command execution. Delegates to the
bridge, chat view, and input panel.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

_log = logging.getLogger(__name__)

from dataclasses import dataclass

from aura.agents.roster import EMPTY_AGENT_ROSTER, AgentTurnRoster
from aura.agents.workflow_plan import WorkflowRunPlan
from aura.config import (
    PROVIDERS,
    AppSettings,
    ModelInfo,
    ThinkingMode,
    has_usable_provider_configuration,
)
from aura.conversation.external_paths import extract_external_read_paths
from aura.conversation.target_files import extract_target_files
from aura.git_ops import (
    recent_commit_log,
    restore_to_snapshot,
    undo_last_commit,
    working_tree_diff,
    working_tree_status,
)
from aura.gui.builtin_commands import classify_built_in_command
from aura.gui.composer_skills import ComposerSkill
from aura.gui.input_panel import Attachment, SendPayload


def _extract_snapshot_sha(text: str) -> str | None:
    """Pull an explicit commit sha out of a restore request, if present."""
    match = re.search(r"\b([0-9a-f]{7,40})\b", str(text or "").lower())
    return match.group(1) if match else None


@dataclass
class QueuedItem:
    """A captured send request waiting for the current production run to finish.

    Each item preserves its own submission-time state so that changing controls
    after queueing does not alter an already queued request.
    """
    text: str
    attachments: list[Attachment]
    model: str
    thinking: ThinkingMode
    selected_skills: tuple[ComposerSkill, ...] = ()
    #: Full definition + effective grant, immutable and deliberately only in
    #: memory. Child instructions never enter canonical/provider History.
    agent_roster: AgentTurnRoster = EMPTY_AGENT_ROSTER
    #: The one workflow this submission may run, already resolved, or None
    #: when the Agents switch was off. Frozen for the same reason the roster
    #: is: a queued turn runs the authority it was queued with.
    workflow_plan: WorkflowRunPlan | None = None


class SendHandler(QObject):
    """Handles send/stop/undo logic extracted from MainWindow.

    Owns the message queue for queuing payloads while the bridge is busy,
    and processes the /undo git command.
    """

    agents_requested = Signal()  # /agents command → open/toggle the Agents page
    skills_manager_requested = Signal()  # /skills command → open the Skills manager
    stop_requested = Signal()  # MainWindow correlation seam; cancellation stays here

    def __init__(
        self,
        bridge,
        chat,
        input_panel,
        settings: AppSettings,
        workspace_root: Path | None,
        parent=None,
        agent_roster_provider=None,
        workflow_plan_provider=None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._chat = chat
        self._input = input_panel
        self._settings = settings
        self._workspace_root = workspace_root
        # The Agents controller owns storage and returns one full immutable
        # submission-time roster. SendHandler never constructs storage owners.
        self._agent_roster_provider = agent_roster_provider
        # Same owner, same rule, for the workflow capability: it answers with
        # a resolved plan or with None, and None is what an Agents switch that
        # is off always produces.
        self._workflow_plan_provider = workflow_plan_provider

        # Queued messages sent while the bridge is running.
        self._message_queue: list[QueuedItem] = []
        self._queue_paused = False

    # ---- public helpers (called externally from MainWindow) -----------------

    def set_workspace_root(self, root: Path | None) -> None:
        """Update the workspace root path (called when user changes root)."""
        self._workspace_root = root

    def update_settings(self, settings: AppSettings) -> None:
        """Use the latest settings object after Settings is accepted."""
        self._settings = settings

    def clear_queue(self) -> None:
        """Clear queued messages and external read authority.

        Called when the active conversation changes (new, opened, or selected
        from the thread list). Authority derived from one conversation's text
        must not survive into another.
        """
        self._message_queue.clear()
        clear_skills = getattr(self._input, "clear_selected_skills", None)
        if callable(clear_skills):
            clear_skills()
        clear = getattr(self._bridge, "clear_external_read_authorization", None)
        if callable(clear):
            clear()

    def process_message_queue(self, model: str, thinking: ThinkingMode) -> None:
        """Send the next queued message, if any."""
        self._process_message_queue(model, thinking)

    def set_queue_paused(self, paused: bool) -> None:
        """Temporarily hold FIFO dequeue without dropping or reordering items."""
        self._queue_paused = bool(paused)

    # ---- public API --------------------------------------------------------

    def set_agent_roster_provider(self, provider) -> None:
        """Wire the Agent controller's submission-time roster producer."""
        self._agent_roster_provider = provider

    def set_workflow_plan_provider(self, provider) -> None:
        """Wire the Agent controller's submission-time workflow producer."""
        self._workflow_plan_provider = provider

    def _capture_workflow_plan(
        self, model: str, thinking: ThinkingMode
    ) -> WorkflowRunPlan | None:
        """Freeze this submission's runnable workflow, if there is one.

        The turn's own model and thinking go in, because the plan resolves
        each step's model under them; a queued item replays the pair it was
        queued with, so its steps run on the model the user actually chose.
        """
        provider = self._workflow_plan_provider
        if provider is None:
            return None
        try:
            plan = provider(model=str(model), thinking=str(thinking))
        except Exception:
            _log.debug("Could not freeze the submitted Agent workflow", exc_info=True)
            return None
        return plan if isinstance(plan, WorkflowRunPlan) else None

    def _capture_agent_roster(self) -> AgentTurnRoster:
        """Read one immutable roster from the injected storage owner."""
        provider = self._agent_roster_provider
        if provider is None:
            return EMPTY_AGENT_ROSTER
        try:
            roster = provider()
            return roster if isinstance(roster, AgentTurnRoster) else EMPTY_AGENT_ROSTER
        except Exception:
            _log.debug("Could not freeze the submitted Agent roster", exc_info=True)
            return EMPTY_AGENT_ROSTER

    def handle_send(
        self,
        payload: SendPayload,
        model: str,
        thinking: ThinkingMode,
        agent_roster: AgentTurnRoster | None = None,
        workflow_plan: WorkflowRunPlan | None = None,
        replaying: bool = False,
    ) -> bool:
        """Process a send payload: run Aura's own commands, queue if busy, or send.

        Anything that is not one of Aura's literal commands is an ordinary
        message and goes to the production loop as the user wrote it.
        """
        # Guard: no workspace selected
        if self._workspace_root is None:
            self._chat.add_error(
                "No workspace",
                "Open a project first. Try the Demo Project to test Aura safely, or open an existing project folder.",
            )
            return False

        built_in = classify_built_in_command(payload.text)

        # Browsing skills is a local view over what is already installed. It
        # never reaches a model, so it works before any provider is set up,
        # and it leaves no trace in the conversation: no chat bubble, no
        # History entry, no manager dump.
        if built_in == "skills":
            self._restore_local_command_selection(payload)
            self.skills_manager_requested.emit()
            return False

        # Same for the Agents page: it is a local view over definitions and
        # this user's own grants. It opens (or closes) the page the rail
        # button opens, before any provider is configured, and leaves the
        # conversation untouched — no bubble, no History entry, no request.
        if built_in == "agents_enter_mode":
            self._restore_local_command_selection(payload)
            self.agents_requested.emit()
            return False

        # Guard: no provider configured
        if not has_usable_provider_configuration(self._settings.provider):
            provider_cfg = PROVIDERS.get(self._settings.provider)
            if provider_cfg is not None and provider_cfg.kind == "local":
                detail = (
                    "Start your OpenAI-compatible local server, then use "
                    "Settings → Models → Test / Discover and select a model.\n\n"
                    "You can still open and browse a project folder without a model."
                )
            else:
                detail = (
                    "Configure a hosted provider in Settings → API Keys, or a local "
                    "OpenAI-compatible endpoint in Settings → Models. DeepSeek, OpenAI, "
                    "Anthropic, Gemini, OpenRouter, and local models are supported.\n\n"
                    "You can also open/browse a project folder before configuring AI."
                )
            self._chat.add_error(
                "No AI provider configured",
                detail,
            )
            return False

        if built_in is not None:
            self._restore_local_command_selection(payload)
            self._chat.add_user(payload.text)
            self._handle_built_in_action(built_in, payload.text)
            return False

        # Frozen once for this submission: a queued item keeps the roster it
        # was queued with, and a dequeued one replays that captured roster
        # rather than reading the live one again.
        frozen_roster = (
            self._capture_agent_roster()
            if agent_roster is None
            else agent_roster
        )
        frozen_workflow = (
            workflow_plan if replaying else self._capture_workflow_plan(model, thinking)
        )

        if self._bridge.is_running() or self._queue_paused:
            item = QueuedItem(
                text=payload.text,
                attachments=list(payload.attachments),
                model=model,
                thinking=thinking,
                selected_skills=tuple(payload.selected_skills),
                agent_roster=frozen_roster,
                workflow_plan=frozen_workflow,
            )
            self._message_queue.append(item)
            self._input.set_queued_messages(len(self._message_queue))
            return False

        image_atts = [a for a in payload.attachments if a.kind == "image" and a.b64]
        if image_atts and not self._model_supports_vision(model):
            self._input.restore_payload(payload)
            self._chat.add_error(
                "Unsupported model",
                f"{model} does not support image input. Select a vision-capable "
                "model to send images, or remove the attached image(s).",
            )
            return False

        self._finalize_send(
            payload, model, thinking, frozen_roster, frozen_workflow
        )
        return True

    def _restore_local_command_selection(self, payload: SendPayload) -> None:
        """Give back the skill chips a local command never spent.

        The composer clears itself the moment it emits a payload, but one of
        Aura's own commands runs locally instead of consuming a model turn,
        so the skills the user picked for their *next* message are still
        waiting to be sent. The command text is deliberately not restored:
        the command did run.
        """
        if not payload.selected_skills:
            return
        restore = getattr(self._input, "restore_selected_skills", None)
        if callable(restore):
            restore(tuple(payload.selected_skills))

    def handle_stop(self, *, preserve_queue: bool = False) -> None:
        """Cancel the response and normally clear queued messages.

        A creation turn preserves queued messages for normal FIFO processing
        after its review/install session ends. The composer draft and
        attachments are always preserved.
        """
        self.stop_requested.emit()
        self._bridge.request_cancel()
        if not (preserve_queue or self._queue_paused):
            self._message_queue.clear()
            self._input.set_queued_messages(0)

    def handle_retry_last(
        self,
        model: str,
        thinking: ThinkingMode,
        replay_cb=None,
    ) -> bool:
        """Rerun the most recent user turn after discarding its response."""
        if self._bridge.is_running() or self._queue_paused:
            return False

        rewound = self._bridge.history.rewind_to_last_user_turn()
        if not rewound:
            self._chat.add_error("Retry", "No user message to retry.")
            return False

        # External read authority comes from the literal composer text stored
        # on the retained user message and from nothing else. That value is
        # durable across conversation switches, restarts, and restored history,
        # and there is deliberately no fallback to the retained message content:
        # that content also carries attachment reference blocks Aura generated,
        # which were never user-authored path authority. A message with no
        # literal-composer metadata therefore authorizes nothing.
        self._authorize_external_reads_for_turn(
            self._bridge.history.latest_real_user_literal_composer_text()
        )
        # Target files stay derived from the retained user text, unchanged.
        self._declare_turn_target_files(self._bridge.history.latest_real_user_text())

        self._message_queue.clear()
        self._input.set_queued_messages(0)
        self._chat.reset()
        if replay_cb is not None:
            replay_cb()
        self._chat.begin_assistant()
        set_roster = getattr(self._bridge, "set_submitted_agent_roster", None)
        if callable(set_roster):
            set_roster(self._capture_agent_roster())
        self._set_submitted_workflow(self._capture_workflow_plan(model, thinking))
        self._bridge.send(model=model, thinking=thinking)
        return True

    def _authorize_external_reads_for_turn(self, raw_user_text: str | None) -> None:
        """Derive this turn's external read allowlist from the literal user text.

        Only absolute paths the user typed themselves can authorize a read
        outside the workspace, so this reads the composer text (or, for a retry,
        the literal composer text stored on the retained user message) and
        nothing else — never attachment metadata, generated descriptions, model
        output, or tool arguments. It runs before ``bridge.send()``, and it
        always runs: a turn that named nothing replaces the previous turn's
        allowlist with an empty one.
        """
        authorize = getattr(self._bridge, "authorize_external_reads", None)
        if not callable(authorize):
            # Lightweight bridge doubles used by non-production callers may
            # predate this optional seam; the real ConversationBridge always
            # supplies it.
            return
        if self._workspace_root is None:
            authorize(())
            return
        paths = extract_external_read_paths(raw_user_text, self._workspace_root)
        authorized = authorize(paths)
        if authorized:
            _log.info(
                "turn_external_reads %s", ", ".join(str(path) for path in authorized)
            )

    def _declare_turn_target_files(self, text: str | None) -> None:
        """Hand this turn's explicitly named files to the bridge.

        Always called before a send, including when nothing was named: the
        empty tuple clears the previous turn's targets so scope from an
        earlier request cannot leak into this one.
        """
        target_files = extract_target_files(text, self._workspace_root)
        if target_files:
            _log.info("turn_target_files %s", ", ".join(target_files))
        self._bridge.set_turn_target_files(target_files)

    # ---- undo --------------------------------------------------------------
    def _handle_built_in_action(self, action: str, text: str = "") -> None:
        """Run one of Aura's own literal commands locally, without the model."""
        if action == "undo":
            self._handle_undo()
            return
        if action == "restore_snapshot":
            self._handle_restore_snapshot(text)
            return
        if action == "git_status":
            self._handle_git_status()
            return
        if action == "git_diff":
            self._handle_git_diff()
            return
        if action == "git_log":
            self._handle_git_log()
            return
        self._chat.add_error("Built-in action", f"Unsupported action: {action}")

    def _handle_restore_snapshot(self, text: str = "") -> None:
        """Restore an explicitly named snapshot, or list the ones available."""
        if self._bridge.is_running():
            self._chat.add_error(
                "Restore snapshot",
                "Stop the running task before undoing or restoring a snapshot.",
            )
            return

        ws_root = self._workspace_root
        if ws_root is None:
            self._chat.add_error("Restore snapshot", "No workspace root set.")
            return

        sha = _extract_snapshot_sha(text)
        if sha:
            reply = QMessageBox.question(
                self._chat,
                "Restore Snapshot",
                f"This will discard ALL changes made after {sha}. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._chat.add_info("Restore snapshot", "Cancelled.")
                return
            ok, message = restore_to_snapshot(ws_root, sha)
            if ok:
                self._bridge.clear_pre_execution_snapshot()
                self._chat.add_info("Restore snapshot", message)
            else:
                self._chat.add_error("Restore snapshot", message)
            return

        # No sha given — show what can actually be restored to.
        lines = ["Name the snapshot to restore, e.g. `restore snapshot a1b2c3d`."]
        pre_execution = self._bridge.get_pre_execution_snapshot()
        if pre_execution:
            lines.append(
                f"\nPre-execution snapshot: {pre_execution} (or use /undo to return to it)."
            )
        ok, log_text, message = recent_commit_log(ws_root)
        if ok and log_text.strip():
            lines.append(f"\nRecent commits:\n{log_text.strip()}")
        elif not ok:
            lines.append(f"\nCould not read git history: {message}")
        self._chat.add_info("Restore snapshot", "\n".join(lines))

    def _handle_undo(self) -> None:
        """Handle /undo command — restore to pre-execution snapshot or git reset."""
        ws_root = self._workspace_root
        if self._bridge.is_running():
            self._chat.add_error(
                "Undo",
                "Stop the running task before undoing or restoring a snapshot.",
            )
            return
        if ws_root is None:
            self._chat.add_error("Undo", "No workspace root set.")
            return

        # Check for pre-execution snapshot first (more reliable)
        snapshot_sha = self._bridge.get_pre_execution_snapshot()
        if snapshot_sha is not None:
            # Confirm destructive restore
            reply = QMessageBox.question(
                self._chat,  # parent widget (ChatView is a QWidget)
                "Restore to Pre-Execution State",
                "This will discard ALL changes since the execution started "
                "(including any intervening commits). Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                ok, message = restore_to_snapshot(ws_root, snapshot_sha)
                self._bridge.clear_pre_execution_snapshot()
                if ok:
                    self._chat.add_info("Undo", message)
                else:
                    self._chat.add_error("Undo", message)
        else:
            # Fall back to simple undo_last_commit
            ok, message = undo_last_commit(ws_root)
            if ok:
                self._chat.add_info("Undo", message)
            else:
                self._chat.add_error("Undo", message)

    def _handle_git_status(self) -> None:
        ws_root = self._workspace_root
        if ws_root is None:
            self._chat.add_error("Git status", "No workspace root set.")
            return

        ok, status, message = working_tree_status(ws_root)
        if ok:
            self._chat.add_info("Git status", status or "Working tree clean.")
        else:
            self._chat.add_error("Git status", message)

    def _handle_git_diff(self) -> None:
        ws_root = self._workspace_root
        if ws_root is None:
            self._chat.add_error("Git diff", "No workspace root set.")
            return

        ok, diff, message = working_tree_diff(ws_root)
        if ok:
            self._chat.add_info("Git diff", diff or "No unstaged changes.")
        else:
            self._chat.add_error("Git diff", message)

    def _handle_git_log(self) -> None:
        ws_root = self._workspace_root
        if ws_root is None:
            self._chat.add_error("Git log", "No workspace root set.")
            return

        ok, log_text, message = recent_commit_log(ws_root)
        if ok:
            self._chat.add_info("Git log", log_text or "No commits found.")
        else:
            self._chat.add_error("Git log", message)

    # ---- finalise send -----------------------------------------------------

    def _finalize_send(
        self,
        payload: SendPayload,
        model: str,
        thinking: ThinkingMode,
        agent_roster: AgentTurnRoster = EMPTY_AGENT_ROSTER,
        workflow_plan: WorkflowRunPlan | None = None,
    ) -> None:
        """Build the message parts, append to history, and send via the bridge."""
        # Authorization is derived from the literal user text before history
        # mutation or bridge.send().
        self._authorize_external_reads_for_turn(payload.text)

        image_atts = [a for a in payload.attachments if a.kind == "image" and a.b64]
        text = payload.text
        text_refs = [a.text_ref for a in payload.attachments if a.text_ref]
        if text_refs:
            ref_block = "\n".join(text_refs)
            text = f"{text}\n\n{ref_block}".strip() if text else ref_block

        if image_atts:
            # Native multimodal turn: ordered text + image_url content parts.
            parts = []
            if text:
                parts.append({"type": "text", "text": text})
            for a in image_atts:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{a.b64}"},
                })
            self._bridge.history.append_user_multimodal(
                parts,
                literal_composer_text=payload.text,
                explicit_installed_skill_ids=tuple(
                    skill.install_id for skill in payload.selected_skills
                ),
                available_agent_ids=agent_roster.ids,
            )
        else:
            self._bridge.history.append_user_text(
                text,
                literal_composer_text=payload.text,
                explicit_installed_skill_ids=tuple(
                    skill.install_id for skill in payload.selected_skills
                ),
                available_agent_ids=agent_roster.ids,
            )

        self._chat.add_user(text, [a.b64 for a in image_atts] or None)
        self._chat.scroll_to_bottom(force=True)
        self._chat.begin_assistant()

        self._declare_turn_target_files(text)

        _log.info(
            "send_start model=%s thinking=%s workspace_root=%s",
            model, thinking, self._workspace_root,
        )
        set_roster = getattr(self._bridge, "set_submitted_agent_roster", None)
        if callable(set_roster):
            set_roster(agent_roster)
        self._set_submitted_workflow(workflow_plan)
        self._bridge.send(model=model, thinking=thinking)

    def _set_submitted_workflow(self, plan: WorkflowRunPlan | None) -> None:
        """Hand the bridge this submission's workflow, always — None included.

        Setting it unconditionally is what makes the switch a real gate: a
        turn submitted with Agents off deposits nothing, so the next turn
        cannot inherit the last one's workflow.
        """
        setter = getattr(self._bridge, "set_submitted_workflow_plan", None)
        if callable(setter):
            setter(plan)

    # ---- model info lookup -------------------------------------------------

    def _get_current_model_info(self, model: str) -> ModelInfo | None:
        """Look up metadata for the model from the provider used for chat sends."""
        cfg = PROVIDERS.get(self._settings.provider)
        if not cfg:
            return None
        return cfg.models.get(model)

    def _model_supports_vision(self, model: str) -> bool:
        m_info = self._get_current_model_info(model)
        return m_info.supports_vision if m_info else False

    # ---- message queue -----------------------------------------------------

    def _process_message_queue(self, model: str, thinking: ThinkingMode) -> None:
        """Send the next queued message, if any."""
        if self._bridge.is_running():
            return
        if self._queue_paused:
            return
        if not self._message_queue:
            return
        item = self._message_queue.pop(0)
        self._input.set_queued_messages(len(self._message_queue))
        # Reconstruct a SendPayload from the queued item, which captured its
        # own model and thinking at queue time.
        payload = SendPayload(
            text=item.text,
            attachments=list(item.attachments),
            selected_skills=tuple(item.selected_skills),
        )
        self.handle_send(
            payload,
            item.model,
            item.thinking,
            agent_roster=item.agent_roster,
            workflow_plan=item.workflow_plan,
            replaying=True,
        )
