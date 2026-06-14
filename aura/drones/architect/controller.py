from __future__ import annotations

import logging
from pathlib import Path

from aura.drones.architect.build_prompts import (
    build_candidate_dispatch_prompt,
    build_repair_dispatch_prompt,
)
from aura.drones.architect.commands import DroneCommand, parse_drone_command
from aura.drones.architect.results import (
    AwaitingDecision,
    BuildCompleted,
    BuildFailed,
    BuildStarted,
    Discarded,
    ErrorResult,
    Installed,
    ModeEntered,
    ProofCompleted,
    ProofResult,
    ReadinessFailed,
    ReadinessPassed,
    WorkshopClarifying,
    WorkshopQuestion,
    WorkshopRequested,
    WorkspaceLoaded,
)
from aura.drones.architect.workshop_prompt import build_workshop_messages
from aura.drones.store import DroneStore
from aura.drones.workshop_runner import DroneWorkshopResponse
from aura.drones.workspaces.model import DroneWorkspace, WorkspacePhase
from aura.drones.workspaces.paths import candidate_dir
from aura.drones.workspaces.store import DroneWorkspaceStore

logger = logging.getLogger(__name__)


class DroneArchitectController:
    """Owns the Drone authoring lifecycle state machine. Pure Python, no Qt."""

    def __init__(self):
        self._workspace_root: Path | None = None
        self._active_workspace: DroneWorkspace | None = None
        self._workshop_conversation: list[dict[str, str]] = []
        self._pending_dispatch_spec: dict | None = None
        self._last_candidate_path: str = ""
        self._last_drone_id: str = ""
        self._last_proof_result: ProofResult | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active_workspace(self) -> DroneWorkspace | None:
        return self._active_workspace

    @property
    def phase(self) -> str:
        if self._active_workspace is None:
            return "inactive"
        return self._active_workspace.phase

    @property
    def is_active(self) -> bool:
        return self._active_workspace is not None

    @property
    def workshop_messages(self) -> list[dict[str, str]]:
        return list(self._workshop_conversation)

    @property
    def pending_dispatch_spec(self) -> dict | None:
        return self._pending_dispatch_spec

    @property
    def last_candidate_path(self) -> str:
        return self._last_candidate_path

    @property
    def last_drone_id(self) -> str:
        return self._last_drone_id

    @property
    def has_installed_drone(self) -> bool:
        ws = self._active_workspace
        return ws is not None and bool(ws.installed_drone_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_workspace_root(self, root: Path | None) -> None:
        """Set workspace root. Clears active workspace if root changes."""
        if root != self._workspace_root:
            self._workspace_root = root
            self._active_workspace = None
            self._workshop_conversation = []
            self._pending_dispatch_spec = None

    def enter_mode(self):
        """Enter Drone mode. Try to load active workspace; return ModeEntered if none."""
        if self._workspace_root is None:
            return ModeEntered(workspace_id=None, display_name=None)
        ws = DroneWorkspaceStore.load_active_workspace(self._workspace_root)
        if ws is not None:
            self._active_workspace = ws
            return WorkspaceLoaded(
                workspace_id=ws.workspace_id,
                display_name=ws.display_name,
                phase=ws.phase,
            )
        return ModeEntered(workspace_id=None, display_name=None)

    def exit_mode(self) -> None:
        """Reset internal state."""
        self._active_workspace = None
        self._workshop_conversation = []
        self._pending_dispatch_spec = None
        self._last_candidate_path = ""
        self._last_drone_id = ""

    def load_workspace(self, workspace_id: str):
        """Load a workspace by ID."""
        if self._workspace_root is None:
            return ErrorResult(message="No workspace root set")
        ws = DroneWorkspaceStore.load_workspace(self._workspace_root, workspace_id)
        if ws is None:
            return ErrorResult(message=f"Workspace not found: {workspace_id}")
        self._active_workspace = ws
        DroneWorkspaceStore.set_active_workspace(self._workspace_root, ws)
        return WorkspaceLoaded(
            workspace_id=ws.workspace_id,
            display_name=ws.display_name,
            phase=ws.phase,
        )

    def create_workspace(self, display_name: str = "New Drone"):
        """Create a new workspace in workshop phase."""
        if self._workspace_root is None:
            return ErrorResult(message="No workspace root set")
        ws = DroneWorkspaceStore.create_workspace(
            self._workspace_root, display_name
        )
        self._active_workspace = ws
        self._workshop_conversation = []
        self._pending_dispatch_spec = None
        DroneWorkspaceStore.set_active_workspace(self._workspace_root, ws)
        return WorkspaceLoaded(
            workspace_id=ws.workspace_id,
            display_name=ws.display_name,
            phase=ws.phase,
        )

    def load_drone_workspace(self, drone_id: str):
        """Load or create an edit workspace for an installed Drone."""
        if self._workspace_root is None:
            return ErrorResult(message="No workspace root set")

        ws = DroneWorkspaceStore.load_or_create_workspace_for_drone(
            self._workspace_root, drone_id
        )
        if ws is None:
            return ErrorResult(message=f"Drone not found: {drone_id}")

        self._active_workspace = ws
        self._workshop_conversation = []
        DroneWorkspaceStore.set_active_workspace(self._workspace_root, ws)
        return WorkspaceLoaded(
            workspace_id=ws.workspace_id,
            display_name=ws.display_name,
            phase=ws.phase,
        )

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def handle_user_message(self, text: str):
        """Route user message based on current workspace phase."""
        if self._active_workspace is None:
            return self._ensure_active_workspace(text)

        phase = self._active_workspace.phase

        if phase == WorkspacePhase.WORKSHOP.value:
            return self._handle_workshop_message(text)

        if phase == WorkspacePhase.BUILDING.value:
            return ErrorResult(message="Build already in progress")

        if phase in (
            WorkspacePhase.READINESS_FAILED.value,
            WorkspacePhase.PROOF_FAILED.value,
        ):
            return self._handle_failed_phase_message(text)

        if phase == WorkspacePhase.AWAITING_DECISION.value:
            return self._handle_decision_message(text)

        if phase == WorkspacePhase.ITERATING.value:
            return self._handle_iterating_message(text)

        return ErrorResult(message=f"Unhandled phase: {phase}")

    def handle_workshop_response(
        self, ws_response: DroneWorkshopResponse, raw_text: str
    ):
        """Process parsed workshop response. Return next result."""
        if self._active_workspace is None:
            return ErrorResult(message="No active workspace")

        self._workshop_conversation.append(
            {"role": "assistant", "content": raw_text}
        )

        if ws_response.kind == "question":
            return WorkshopQuestion(message=ws_response.message)

        if ws_response.kind == "brief":
            brief = ws_response.brief
            if brief is None:
                return WorkshopClarifying(message=ws_response.message)

            if brief.ready_to_build and brief.build_brief.strip():
                self._active_workspace.build_brief = brief.build_brief
                self._active_workspace.phase = WorkspacePhase.BUILDING.value
                DroneWorkspaceStore.save_workspace(self._active_workspace)

                dispatch_spec = build_candidate_dispatch_prompt(
                    self._active_workspace, brief
                )
                self._pending_dispatch_spec = dispatch_spec

                return BuildStarted(
                    build_brief=brief.build_brief,
                    dispatch_spec=dispatch_spec,
                )

            return WorkshopClarifying(message=ws_response.message)

        if ws_response.kind == "error":
            return ErrorResult(message=ws_response.message)

        return ErrorResult(
            message=f"Unknown workshop response kind: {ws_response.kind}"
        )

    def on_build_completed(self, success: bool, error: str | None = None):
        """Called after Worker finishes building. Triggers readiness."""
        if self._active_workspace is None:
            return ErrorResult(message="No active workspace")

        if success:
            self._active_workspace.phase = WorkspacePhase.READINESS_RUNNING.value
            DroneWorkspaceStore.save_workspace(self._active_workspace)

            # Extract drone_id from candidate drone.json.
            drone_id = ""
            candidate_path = ""
            if self._workspace_root is not None:
                project_root = Path(self._active_workspace.project_root)
                cand = candidate_dir(
                    project_root, self._active_workspace.workspace_id
                )
                candidate_path = str(cand)
                try:
                    drone = DroneStore.load_drone_from_folder(cand)
                    drone_id = drone.id
                except Exception:
                    logger.warning(
                        "Could not load drone.json from candidate %s", cand
                    )

            self._last_candidate_path = candidate_path
            self._last_drone_id = drone_id

            return BuildCompleted(
                candidate_path=candidate_path, drone_id=drone_id
            )
        else:
            self._active_workspace.phase = WorkspacePhase.READINESS_FAILED.value
            self._active_workspace.last_error = error
            DroneWorkspaceStore.save_workspace(self._active_workspace)
            return BuildFailed(error=error or "Unknown build error")

    def on_readiness_completed(self, result: dict):
        """Called after readiness check. Triggers proof if passed."""
        if self._active_workspace is None:
            return ErrorResult(message="No active workspace")

        if result.get("ok"):
            self._active_workspace.phase = WorkspacePhase.PROOF_RUNNING.value
            self._active_workspace.last_readiness_result = result
            DroneWorkspaceStore.save_workspace(self._active_workspace)
            return ReadinessPassed(result=result)
        else:
            self._active_workspace.phase = WorkspacePhase.READINESS_FAILED.value
            self._active_workspace.last_error = result.get(
                "error", "Unknown readiness error"
            )
            self._active_workspace.last_readiness_result = result
            DroneWorkspaceStore.save_workspace(self._active_workspace)
            return ReadinessFailed(
                error=result.get("error", ""), detail=result
            )

    def on_proof_completed(self, proof_result: ProofResult):
        """Called after proof run. Transitions based on proof status."""
        if self._active_workspace is None:
            return ErrorResult(message="No active workspace")

        self._last_proof_result = proof_result

        if proof_result.proof_status == "failed":
            self._active_workspace.phase = WorkspacePhase.PROOF_FAILED.value
        else:
            self._active_workspace.phase = WorkspacePhase.AWAITING_DECISION.value
        DroneWorkspaceStore.save_workspace(self._active_workspace)

        return ProofCompleted(proof_result=proof_result)

    def finalize_proof(self):
        """Finalize proof — gate on failed, or build AwaitingDecision."""
        if self._active_workspace is None:
            return ErrorResult(message="No active workspace")

        if self._active_workspace.phase == WorkspacePhase.PROOF_FAILED.value:
            return ErrorResult(
                message="Proof failed. Revise the Drone and rebuild before installing."
            )

        pr = self._last_proof_result
        proof_summary = (
            f"Status: {pr.proof_status}\n"
            f"Tried: {pr.what_tried}\n"
        )
        if pr.errors:
            proof_summary += f"Errors: {', '.join(pr.errors)}\n"
        if pr.warnings:
            proof_summary += f"Warnings: {', '.join(pr.warnings)}\n"

        return AwaitingDecision(
            workspace_id=self._active_workspace.workspace_id,
            drone_name=self._active_workspace.display_name,
            proof_summary=proof_summary,
        )

    # ------------------------------------------------------------------
    # Terminal actions
    # ------------------------------------------------------------------

    def install_candidate(self):
        """Install candidate into global drone registry."""
        if self._active_workspace is None or self._workspace_root is None:
            return ErrorResult(message="No active workspace")

        from aura.drones.architect.installer import install_or_reinstall

        result = install_or_reinstall(self._active_workspace, self._workspace_root)

        if result["ok"]:
            self._active_workspace.phase = "installed"
            DroneWorkspaceStore.save_workspace(self._active_workspace)
            return Installed(
                drone_id=result["drone_id"], drone_name=result["drone_name"]
            )
        else:
            self._active_workspace.last_error = result.get("error", "")
            DroneWorkspaceStore.save_workspace(self._active_workspace)
            return ErrorResult(
                message=f"Install failed: {result.get('error', '')}"
            )

    def discard_workspace(self):
        """Mark workspace as discarded."""
        if self._active_workspace is None:
            return ErrorResult(message="No active workspace")

        workspace_id = self._active_workspace.workspace_id
        DroneWorkspaceStore.discard_workspace(self._active_workspace)
        self._active_workspace = None
        return Discarded(workspace_id=workspace_id)

    # ------------------------------------------------------------------
    # Internal phase handlers
    # ------------------------------------------------------------------

    def _ensure_active_workspace(self, text: str | None = None):
        """Auto-create or load a workspace if none is active."""
        if self._workspace_root is None:
            return ErrorResult(message="No workspace root set")
        ws = DroneWorkspaceStore.load_active_workspace(self._workspace_root)
        if ws is not None:
            self._active_workspace = ws
            self._workshop_conversation = []
            return WorkspaceLoaded(
                workspace_id=ws.workspace_id,
                display_name=ws.display_name,
                phase=ws.phase,
            )
        result = self.create_workspace("New Drone")
        if text:
            self._workshop_conversation.append({"role": "user", "content": text})
            messages = build_workshop_messages(text, self._workshop_conversation[:-1])
            return WorkshopRequested(messages=messages)
        return result

    def _handle_workshop_message(self, text: str):
        cmd, arg = parse_drone_command(text, self._active_workspace.phase)

        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")

        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a workspace name")

        if cmd == DroneCommand.HELP:
            return WorkshopQuestion(
                message=(
                    "I'm helping you design a Drone. Tell me what you want "
                    "it to do — for example: \"remind me when a CI build "
                    'fails" or "fetch the latest PRs and summarize them". '
                    "You can also say 'new' to start over, or 'load <name>' "
                    "to switch to another workspace."
                )
            )

        # UNKNOWN or any other text — treat as workshop conversation.
        self._workshop_conversation.append({"role": "user", "content": text})
        messages = build_workshop_messages(text, self._workshop_conversation[:-1])
        return WorkshopRequested(messages=messages)

    def _handle_failed_phase_message(self, text: str):
        """Handle messages in readiness_failed / proof_failed phases."""
        cmd, arg = parse_drone_command(text, self._active_workspace.phase)

        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")

        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a workspace name")

        if cmd == DroneCommand.INSTALL:
            return ErrorResult(
                message="Cannot install — proof failed. Revise first."
            )

        if cmd == DroneCommand.DISCARD:
            return self.discard_workspace()

        # Any other text — treat as revision feedback.
        self._active_workspace.phase = WorkspacePhase.ITERATING.value
        DroneWorkspaceStore.save_workspace(self._active_workspace)

        repair_spec = build_repair_dispatch_prompt(self._active_workspace, text)
        self._pending_dispatch_spec = repair_spec

        return BuildStarted(
            build_brief=self._active_workspace.build_brief,
            dispatch_spec=repair_spec,
        )

    def _handle_decision_message(self, text: str):
        """Handle messages in awaiting_decision phase."""
        cmd, arg = parse_drone_command(text, self._active_workspace.phase)

        if cmd == DroneCommand.INSTALL:
            return self.install_candidate()

        if cmd == DroneCommand.DISCARD:
            return self.discard_workspace()

        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")

        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a workspace name")

        # REVISE or UNKNOWN — implicit revision.
        revision_text = arg if cmd == DroneCommand.REVISE else text
        self._active_workspace.phase = WorkspacePhase.ITERATING.value
        DroneWorkspaceStore.save_workspace(self._active_workspace)

        repair_spec = build_repair_dispatch_prompt(
            self._active_workspace, revision_text
        )
        self._pending_dispatch_spec = repair_spec

        return BuildStarted(
            build_brief=self._active_workspace.build_brief,
            dispatch_spec=repair_spec,
        )

    def _handle_iterating_message(self, text: str):
        """Handle messages in iterating phase — same as decision but always revises."""
        cmd, arg = parse_drone_command(text, self._active_workspace.phase)

        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")

        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a workspace name")

        if cmd == DroneCommand.DISCARD:
            return self.discard_workspace()

        # Any other text — revision feedback.
        revision_text = arg if cmd == DroneCommand.REVISE else text
        repair_spec = build_repair_dispatch_prompt(
            self._active_workspace, revision_text
        )
        self._pending_dispatch_spec = repair_spec

        return BuildStarted(
            build_brief=self._active_workspace.build_brief,
            dispatch_spec=repair_spec,
        )
