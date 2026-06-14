from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from aura.drones.workspaces.model import DroneWorkspace, WorkspacePhase
from aura.drones.workspaces.paths import candidate_dir
from aura.drones.workspaces.store import DroneWorkspaceStore

if TYPE_CHECKING:
    from aura.drones.workshop_runner import DroneWorkshopResponse

logger = logging.getLogger(__name__)

_AUTO_RESUME_BLOCKED_PHASES = {
    WorkspacePhase.READINESS_FAILED.value,
    WorkspacePhase.PROOF_FAILED.value,
    WorkspacePhase.INSTALLED.value,
    WorkspacePhase.DISCARDED.value,
}
_UNKNOWN_BUILD_ERROR_PLACEHOLDER = "Unknown " + "build error"
_BUILD_FAILURE_WITHOUT_WORKER_DETAIL = (
    "Build failed without an error message from the Worker."
)


def build_failure_error_text(
    *,
    summary: str | None = None,
    status: str | None = None,
    metadata: Any = None,
    exception_text: str | None = None,
    worker_result_detail: Any = None,
    stored_build_result: Any = None,
) -> str:
    """Return the best available user-facing build failure detail."""
    candidates: list[str] = []
    candidates.extend(_failure_candidates_from_any(exception_text))
    candidates.extend(_failure_candidates_from_any(worker_result_detail))
    candidates.extend(_failure_candidates_from_any(metadata))
    candidates.extend(_failure_candidates_from_any(stored_build_result))
    candidates.extend(_failure_candidates_from_any(summary))
    if status_text := _clean_failure_text(status):
        candidates.append(f"Worker status: {status_text}")

    seen: set[str] = set()
    for candidate in candidates:
        text = _clean_failure_text(candidate)
        if not text or text.casefold() == _UNKNOWN_BUILD_ERROR_PLACEHOLDER.casefold():
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        return text
    return _BUILD_FAILURE_WITHOUT_WORKER_DETAIL


def _failure_candidates_from_any(value: Any) -> list[str]:
    if value is None or value is False:
        return []
    if isinstance(value, str):
        text = _clean_failure_text(value)
        return [text] if text else []
    if hasattr(value, "to_tool_payload") and callable(value.to_tool_payload):
        try:
            return _failure_candidates_from_any(value.to_tool_payload())
        except Exception:
            pass
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _failure_candidates_from_any(vars(value))
    if isinstance(value, dict):
        return _failure_candidates_from_mapping(value)
    if isinstance(value, (list, tuple, set)):
        candidates: list[str] = []
        for item in value:
            candidates.extend(_failure_candidates_from_any(item))
        return candidates
    text = _clean_failure_text(value)
    return [text] if text else []


def _failure_candidates_from_mapping(data: dict[str, Any]) -> list[str]:
    candidates: list[str] = []

    extras = data.get("extras")
    if isinstance(extras, dict):
        candidates.extend(_failure_candidates_from_mapping(extras))

    for nested_key in (
        "metadata",
        "worker_result",
        "worker_result_detail",
        "dispatch_result",
        "stored_build_result",
        "build_result",
    ):
        nested = data.get(nested_key)
        if nested is not data:
            candidates.extend(_failure_candidates_from_any(nested))

    for key in (
        "internal_error",
        "exception_text",
        "exception",
        "error",
        "message",
        "detail",
        "reason",
        "validation",
        "summary",
        "result_summary",
    ):
        candidates.extend(_failure_candidates_from_any(data.get(key)))

    for key in ("errors", "api_errors", "caveats"):
        candidates.extend(_failure_candidates_from_any(data.get(key)))

    for key in ("validation_results", "terminal_results"):
        records = data.get(key)
        if isinstance(records, list):
            for record in records:
                formatted = _format_validation_failure(record)
                if formatted:
                    candidates.append(formatted)

    for key in (
        "failed_tool_results",
        "failed_write_tools",
        "not_applied_writes",
        "unrecovered_not_applied_writes",
        "source_inspection_blockers",
        "terminal_policy_blockers",
        "environment_setup_blockers",
    ):
        candidates.extend(_format_tool_failures(data.get(key)))

    phase_boundary = data.get("phase_boundary")
    if isinstance(phase_boundary, dict):
        candidates.extend(_failure_candidates_from_mapping(phase_boundary))

    if status_text := _clean_failure_text(data.get("status")):
        candidates.append(f"Worker status: {status_text}")

    if not candidates and data:
        compact = _compact_json(data)
        if compact:
            candidates.append(f"Worker result detail: {compact}")

    return candidates


def _format_validation_failure(record: Any) -> str:
    if not isinstance(record, dict):
        return ""
    if record.get("ok") is not False:
        return ""
    command = _clean_failure_text(record.get("command"))
    exit_code = record.get("exit_code")
    output = _clean_failure_text(
        record.get("output")
        or record.get("output_preview")
        or record.get("error")
        or record.get("result_preview")
    )
    parts = ["Validation failed"]
    if command:
        parts.append(command)
    if exit_code is not None:
        parts.append(f"exit code {exit_code}")
    if output:
        parts.append(output.splitlines()[0])
    return ": ".join(parts)


def _format_tool_failures(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    formatted: list[str] = []
    for record in value:
        if not isinstance(record, dict):
            formatted.extend(_failure_candidates_from_any(record))
            continue
        name = _clean_failure_text(record.get("name") or record.get("tool"))
        path = _clean_failure_text(record.get("path") or record.get("rel_path"))
        failure_class = _clean_failure_text(record.get("failure_class"))
        error = _clean_failure_text(
            record.get("error")
            or record.get("reason")
            or record.get("result_preview")
        )
        parts: list[str] = []
        if name:
            parts.append(name)
        if path:
            parts.append(path)
        if failure_class:
            parts.append(failure_class)
        if error:
            parts.append(error)
        if parts:
            formatted.append("Tool failure: " + ": ".join(parts))
    return formatted


def _clean_failure_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return _compact_json(value)
    text = str(value).strip()
    return " ".join(text.split())


def _compact_json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text.strip()[:500]


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
        """Enter Drone mode. Load active non-terminal Builder state if present."""
        if self._workspace_root is None:
            return ModeEntered(workspace_id=None, display_name=None)
        ws = DroneWorkspaceStore.load_active_workspace(self._workspace_root)
        if ws is None:
            self._active_workspace = None
            self._workshop_conversation = []
            self._pending_dispatch_spec = None
            return ModeEntered(workspace_id=None, display_name=None)
        self._active_workspace = ws
        return WorkspaceLoaded(
            workspace_id=ws.workspace_id,
            display_name=ws.display_name,
            phase=ws.phase,
        )

    def _start_workshop_from_text(self, text: str):
        self._workshop_conversation.append({"role": "user", "content": text})
        messages = build_workshop_messages(text, self._workshop_conversation[:-1])
        return WorkshopRequested(messages=messages)

    def _create_workspace_from_first_message(self, text: str):
        """Create the first persisted workspace once the user starts work."""
        cmd, arg = parse_drone_command(text, WorkspacePhase.WORKSHOP.value)
        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")
        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a Drone name")
        if cmd == DroneCommand.HELP:
            return WorkshopQuestion(
                message=(
                    "I'm helping you design a Drone. Tell me what you want "
                    "it to do — for example: \"remind me when a CI build "
                    'fails" or "fetch the latest PRs and summarize them". '
                    "You can also say 'new' to start over, or 'load <name>' "
                    "to switch to another Drone."
                )
            )

        self.create_workspace("New Drone")
        return self._start_workshop_from_text(text)

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
            return ErrorResult(message="No project root set")
        ws = DroneWorkspaceStore.load_workspace(self._workspace_root, workspace_id)
        if ws is None:
            return ErrorResult(message=f"Drone not found: {workspace_id}")
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
            return ErrorResult(message="No project root set")
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
            return ErrorResult(message="No project root set")

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

        if phase in (
            WorkspacePhase.INSTALLED.value,
            WorkspacePhase.DISCARDED.value,
        ):
            self.create_workspace("New Drone")
            return self._handle_workshop_message(text)

        return ErrorResult(message=f"Unhandled phase: {phase}")

    def handle_workshop_response(
        self, ws_response: DroneWorkshopResponse, raw_text: str
    ):
        """Process parsed workshop response. Return next result."""
        if self._active_workspace is None:
            return ErrorResult(message="No active Drone")

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

    def on_build_completed(
        self,
        success: bool,
        error: str | None = None,
        *,
        failure_detail: Any = None,
    ):
        """Called after Worker finishes building. Triggers readiness."""
        if self._active_workspace is None:
            return ErrorResult(message="No active Drone")

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
                    self._active_workspace.candidate_drone_id = drone.id
                    if self._active_workspace.mode != "edit":
                        self._active_workspace.display_name = drone.name
                except Exception:
                    logger.warning(
                        "Could not load drone.json from candidate %s", cand
                    )

            self._last_candidate_path = candidate_path
            self._last_drone_id = drone_id
            DroneWorkspaceStore.save_workspace(self._active_workspace)

            return BuildCompleted(
                candidate_path=candidate_path, drone_id=drone_id
            )
        else:
            failure_error = build_failure_error_text(
                summary=error,
                metadata=failure_detail,
            )
            self._active_workspace.phase = WorkspacePhase.READINESS_FAILED.value
            self._active_workspace.last_error = failure_error
            DroneWorkspaceStore.save_workspace(self._active_workspace)
            return BuildFailed(error=failure_error)

    def on_readiness_completed(self, result: dict):
        """Called after readiness check. Triggers proof if passed."""
        if self._active_workspace is None:
            return ErrorResult(message="No active Drone")

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
            return ErrorResult(message="No active Drone")

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
            return ErrorResult(message="No active Drone")

        if self._active_workspace.phase == WorkspacePhase.PROOF_FAILED.value:
            return ErrorResult(
                message="Test Run failed. Revise the Drone and rebuild."
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
        """Make the current candidate ready for normal Drone launches."""
        if self._active_workspace is None or self._workspace_root is None:
            return ErrorResult(message="No active Drone")

        from aura.drones.architect.installer import install_or_reinstall

        result = install_or_reinstall(self._active_workspace, self._workspace_root)

        if result["ok"]:
            self._active_workspace.phase = WorkspacePhase.INSTALLED.value
            DroneWorkspaceStore.save_workspace(self._active_workspace)
            return Installed(
                drone_id=result["drone_id"], drone_name=result["drone_name"]
            )
        else:
            self._active_workspace.last_error = result.get("error", "")
            self._active_workspace.phase = WorkspacePhase.READINESS_FAILED.value
            DroneWorkspaceStore.save_workspace(self._active_workspace)
            return ErrorResult(
                message=f"Could not make Drone Ready: {result.get('error', '')}"
            )

    def mark_ready_step_started(self) -> None:
        """Mark the active Drone as being finalized for the Ready state."""
        if self._active_workspace is None:
            return
        self._active_workspace.phase = WorkspacePhase.INSTALLING.value
        DroneWorkspaceStore.save_workspace(self._active_workspace)

    def discard_workspace(self):
        """Mark workspace as discarded."""
        if self._active_workspace is None:
            return ErrorResult(message="No active Drone")

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
            return ErrorResult(message="No project root set")
        ws = DroneWorkspaceStore.load_active_workspace(self._workspace_root)
        if ws is not None:
            if ws.phase in _AUTO_RESUME_BLOCKED_PHASES:
                ws = None
            else:
                self._active_workspace = ws
                self._workshop_conversation = []
                if not text:
                    return WorkspaceLoaded(
                        workspace_id=ws.workspace_id,
                        display_name=ws.display_name,
                        phase=ws.phase,
                    )
                # Text provided — route to phase handler.
                phase = ws.phase
                if phase == WorkspacePhase.WORKSHOP.value:
                    return self._handle_workshop_message(text)
                if phase == WorkspacePhase.AWAITING_DECISION.value:
                    return self._handle_decision_message(text)
                # Terminal/unusable phases — start fresh.
                result = self.create_workspace("New Drone")
                if result.kind == "error":
                    return result
                return self._start_workshop_from_text(text)
        if not text:
            return ModeEntered(workspace_id=None, display_name=None)
        return self._create_workspace_from_first_message(text)

    def _handle_workshop_message(self, text: str):
        cmd, arg = parse_drone_command(text, self._active_workspace.phase)

        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")

        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a Drone name")

        if cmd == DroneCommand.HELP:
            return WorkshopQuestion(
                message=(
                    "I'm helping you design a Drone. Tell me what you want "
                    "it to do — for example: \"remind me when a CI build "
                    'fails" or "fetch the latest PRs and summarize them". '
                    "You can also say 'new' to start over, or 'load <name>' "
                    "to switch to another Drone."
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
            return ErrorResult(message="Load requires a Drone name")

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

        if cmd == DroneCommand.DISCARD:
            return self.discard_workspace()

        if cmd == DroneCommand.NEW:
            return self.create_workspace(arg or "New Drone")

        if cmd == DroneCommand.LOAD:
            if arg:
                return self.load_workspace(arg)
            return ErrorResult(message="Load requires a Drone name")

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
            return ErrorResult(message="Load requires a Drone name")

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
