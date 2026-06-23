"""DroneRunner — executes a registered folder-backed Drone on a QThread."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot

from aura.conversation.tools._types import ApprovalDecision
from aura.drones.definition import DroneDefinition
from aura.drones.folder_runner import is_folder_backed_drone, run_folder_drone_sync
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun
from aura.drones.store import DroneStore, RunHistoryStore
from aura.sandbox import SandboxExecutor

if TYPE_CHECKING:
    from aura.bridge.harness_lap_bridge import HarnessLapBridge
    from aura.drones.probes import ProbeFinding


logger = logging.getLogger(__name__)


def _find_owned_dirty_files(
    workspace_root: Path, drone_id: str, dirty_files: set[str]
) -> set[str] | None:
    """Check if dirty files are owned by a previous Gardener run of this drone.

    Returns the set of owned files if ALL dirty files fall within a previous
    run's owned set. Returns an empty set if there are previous runs but none
    of their owned files overlap with dirty_files. Returns None if there are
    no previous Gardener runs (first-time launch with pre-existing dirty files).
    """
    runs = RunHistoryStore.list_runs(workspace_root, limit=30)
    has_prior_runs = False
    for run in runs:
        if run.get("drone_id") != drone_id:
            continue
        has_prior_runs = True
        artifact = run.get("produced_artifact") or {}
        owned = artifact.get("dirty_files_owned")
        if not owned and artifact.get("changed_files"):
            owned = artifact["changed_files"]
        if not owned:
            continue
        owned_set = set(owned)
        if dirty_files.issubset(owned_set):
            return owned_set
    if not has_prior_runs:
        return None
    return set()


class DroneRunner(QObject):
    """Executes a single registered folder-backed Drone on a background thread."""

    statusChanged = Signal(str)
    contentDelta = Signal(str)
    toolCallStart = Signal(int, str, str)
    toolCallArgsDelta = Signal(int, str)
    toolCallEnd = Signal(int)
    toolResult = Signal(str, str, bool, str)
    usageEmitted = Signal(int, int, int, int)
    apiError = Signal(int, str)
    receiptReady = Signal(object)
    approval_requested = Signal(object)
    finished = Signal()

    def __init__(
        self,
        workspace_root: Path,
        drone: DroneDefinition,
        provider_id: str | None = None,
        model: str | None = None,
        auto_approve: bool = False,
        harness_bridge: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._drone = drone
        self._run = DroneRun(drone=drone)
        self._provider = provider_id
        self._model = model
        self._auto_approve = auto_approve
        self._harness_bridge = harness_bridge
        self._run_owned_files: set[str] = set()
        self._lap_target: str | None = None

    def cancel(self) -> None:
        self._run.cancel()

    def _is_harness_lap_drone(self) -> bool:
        if self._drone.kind == "harness-lap":
            return True
        # Fallback: check the manifest on disk directly
        try:
            folder = DroneStore.drone_folder(self._workspace_root, self._drone.id)
            manifest_raw = (folder / "drone.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_raw)
            return manifest.get("kind") == "harness-lap"
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Not a harness-lap drone (%s): %s", self._drone.id, exc)
            return False

    def _build_harness_lap_want(self) -> str:
        instructions = self._drone.instructions or self._drone.description or ""
        budget = self._drone.permissions.get("max_file_lines", 800)
        if budget > 0:
            from aura.drones.probes import probe_file_sizes

            findings = probe_file_sizes(self._workspace_root, budget)
            if findings:
                runs = RunHistoryStore.list_runs(self._workspace_root, limit=50)
                chosen = self._select_lap_target(findings, runs)
                self._lap_target = chosen
                lines = [
                    f"## Size budget probe — {chosen} is over budget (max {budget})",
                    "",
                    f"Read `{chosen}` live and make one bounded, behavior-preserving extraction that reduces it.",
                    "Lift a cohesive unit into its own module with a minimal call left behind.",
                    "Change nothing unrelated.",
                    "",
                ]
                prior = [r for r in runs if r.get("drone_id") == self._drone.id][:10]
                if prior:
                    lines.append("## Recent laps (most recent first) — do not redo or revert")
                    lines.append("")
                    for i, r in enumerate(prior, 1):
                        summary = r.get("summary", "")
                        artifact = r.get("produced_artifact") or {}
                        changed = artifact.get("changed_files", [])
                        suffix = f" [changed: {', '.join(changed)}]" if changed else " [no changes]"
                        lines.append(f"{i}. {summary}{suffix}")
                    lines.append("")
                    lines.append("---")
                    lines.append("")
                lines.append(instructions)
                return "\n".join(lines)
        self._lap_target = None
        runs = RunHistoryStore.list_runs(self._workspace_root, limit=50)
        prior = [r for r in runs if r.get("drone_id") == self._drone.id][:10]
        if not prior:
            return instructions
        lines = ["## Recent laps (most recent first) — do not redo or revert", ""]
        for i, r in enumerate(prior, 1):
            summary = r.get("summary", "")
            artifact = r.get("produced_artifact") or {}
            changed = artifact.get("changed_files", [])
            suffix = f" [changed: {', '.join(changed)}]" if changed else " [no changes]"
            lines.append(f"{i}. {summary}{suffix}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(instructions)
        return "\n".join(lines)

    def _select_lap_target(
        self,
        findings: list[ProbeFinding],
        runs: list[dict],
    ) -> str:
        """Select which over-budget file to target, rotating away from recently-failed targets.

        A target is 'parked' if its most recent attempt ended with status 'failed'.
        Prefers the highest-line-count unparked finding.
        If every finding is parked, picks the one whose most-recent failed attempt is oldest
        (rotate back to the longest-untried hard target).
        """
        # Build dict: target_path -> (most_recent_status, most_recent_started_at)
        # Runs list is most-recent-first, so first encounter per target wins.
        target_latest: dict[str, tuple[str, str]] = {}
        for r in runs:
            if r.get("drone_id") != self._drone.id:
                continue
            artifact = r.get("produced_artifact") or {}
            target = artifact.get("attempted_target")
            if target and target not in target_latest:
                target_latest[target] = (
                    r.get("status", ""),
                    r.get("started_at", ""),
                )

        parked = {t for t, (status, _) in target_latest.items() if status == "failed"}

        # Unparked: findings whose path is not in the parked set
        unparked = [f for f in findings if f.path not in parked]
        if unparked:
            unparked.sort(key=lambda f: f.line_count, reverse=True)
            return unparked[0].path

        # All findings are parked — rotate to the one left alone longest
        def _oldest_attempt(path: str) -> str:
            _, started_at = target_latest.get(path, ("", ""))
            return started_at

        findings.sort(key=lambda f: _oldest_attempt(f.path))
        return findings[0].path

    def _run_harness_lap(self) -> None:
        if self._harness_bridge is None:
            raise RuntimeError("Harness-lap drone requires a bridge but none was provided")

        import datetime as dt

        from aura.conversation.dispatch import WorkerOutcomeStatus
        from aura.git_ops import changes_since, clean_untracked_paths, commit_all, restore_to_snapshot, snapshot, working_tree_status

        # 1. Check working tree — allow owned dirty files from previous Gardener runs
        workspace_root = self._workspace_root
        git_ok, git_output, git_err = working_tree_status(workspace_root)
        dirty_paths: set[str] = set()
        if git_ok and git_output.strip():
            for line in git_output.strip().splitlines():
                raw = line.rstrip()
                if len(raw) > 3:
                    path_part = raw[3:].strip()
                    # Handle renames: "R  old -> new" -> take the "new" part
                    if " -> " in path_part:
                        path_part = path_part.split(" -> ")[-1].strip()
                    if path_part:
                        dirty_paths.add(path_part)
            if dirty_paths:
                owned_from_history = _find_owned_dirty_files(
                    workspace_root, self._drone.id, dirty_paths
                )
                if owned_from_history is not None and dirty_paths == owned_from_history:
                    # All dirty files match a previous Gardener run — auto-continue
                    self._run_owned_files = owned_from_history
                    self.contentDelta.emit(
                        "Continuing Repo Gardener with existing run changes."
                    )
                elif owned_from_history is not None and dirty_paths:
                    # Some or all dirty files are unknown — block with details
                    unknown = dirty_paths - owned_from_history
                    unknown_list = "\n".join(f"  - {f}" for f in sorted(unknown))
                    ended = dt.datetime.now(tz=dt.timezone.utc).isoformat()
                    receipt = DroneReceipt(
                        run_id=self._run.run_id,
                        drone_id=self._drone.id,
                        drone_name=self._drone.name,
                        status="failed",
                        started_at=dt.datetime.fromtimestamp(
                            self._run.started_at, tz=dt.timezone.utc
                        ).isoformat(),
                        ended_at=ended,
                        summary=(
                            "Harness-lap skipped: working tree has unrelated dirty files."
                            f"\nFiles blocking:\n{unknown_list}"
                        ),
                        produced_artifact={
                            "has_work": False,
                            "changed_files": [],
                            "worker_ok": False,
                            "worker_status": "skipped_dirty_tree",
                            "worker_errors": [],
                            "policy_violations": [f"unrelated dirty files: {sorted(unknown)}"],
                            "rollback_status": None,
                        },
                        errors=["Working tree has files not owned by this drone. "
                                "Commit, stash, or revert them before running."],
                        elapsed_seconds=self._run.elapsed_seconds,
                    )
                    RunHistoryStore.save_run(workspace_root, receipt)
                    self.contentDelta.emit(receipt.summary)
                    self._run.mark("failed")
                    self.statusChanged.emit("failed")
                    self.receiptReady.emit(receipt)
                    return
                else:
                    # owned_from_history is None (no previous runs) — block normally
                    dirty_list = "\n".join(f"  - {f}" for f in sorted(dirty_paths))
                    ended = dt.datetime.now(tz=dt.timezone.utc).isoformat()
                    receipt = DroneReceipt(
                        run_id=self._run.run_id,
                        drone_id=self._drone.id,
                        drone_name=self._drone.name,
                        status="failed",
                        started_at=dt.datetime.fromtimestamp(
                            self._run.started_at, tz=dt.timezone.utc
                        ).isoformat(),
                        ended_at=ended,
                        summary=(
                            "Harness-lap skipped: working tree is dirty. "
                            "Commit or stash changes first."
                            f"\nDirty files:\n{dirty_list}"
                        ),
                        produced_artifact={
                            "has_work": False,
                            "changed_files": [],
                            "worker_ok": False,
                            "worker_status": "skipped_dirty_tree",
                            "worker_errors": [],
                            "policy_violations": ["working tree is dirty"],
                            "rollback_status": None,
                        },
                        errors=["Working tree is not clean. Harness-lap requires a clean working tree."],
                        elapsed_seconds=self._run.elapsed_seconds,
                    )
                    RunHistoryStore.save_run(workspace_root, receipt)
                    self.contentDelta.emit(receipt.summary)
                    self._run.mark("failed")
                    self.statusChanged.emit("failed")
                    self.receiptReady.emit(receipt)
                    return
            # else: no dirty paths extracted — proceed (tree is effectively clean)

        self.contentDelta.emit("Preparing unattended lap…")

        # 2. Capture pre-lap snapshot
        pre_sha = snapshot(workspace_root)

        # 3. Read permissions from drone definition (with safe defaults)
        permissions = self._drone.permissions or {}
        permissions.get("require_clean_worktree", True)
        revert_on_failure = permissions.get("revert_on_failure", True)
        max_changed_files = permissions.get("max_changed_files", 0)  # 0 = unlimited
        protected_paths = permissions.get("protected_paths", []) or []
        launch_command = permissions.get("launch_command", "")
        launch_window_seconds = int(permissions.get("launch_window_seconds", 10))

        import fnmatch

        def _path_matches_protected(path: str, patterns: list[str]) -> bool:
            for pat in patterns:
                if fnmatch.fnmatch(path, pat):
                    return True
            return False

        def _revert(sha: str | None) -> tuple[bool, str]:
            if sha:
                ok, msg = restore_to_snapshot(workspace_root, sha)
                return ok, msg
            return False, "No pre-lap snapshot to restore"

        # 4. Run the lap
        want = self._build_harness_lap_want()
        if self._lap_target:
            self.contentDelta.emit(f"**Target:** `{self._lap_target}`")
        self.contentDelta.emit("Running planner → worker lap…")
        lap_result = self._harness_bridge.run_one_lap(want)
        self.contentDelta.emit("Lap complete. Checking results…")

        # 5. Collect outcomes
        changed_files = list(lap_result.changed_files)
        worker_ok = lap_result.worker_ok
        worker_status = lap_result.worker_status
        worker_errors = list(lap_result.worker_errors)
        self._run_owned_files.update(changed_files)

        # --- Repair loop: bounded retry on validation failure ---
        repair_attempt = 0
        max_repairs = int(permissions.get("max_repair_attempts", 5))
        repair_attempts_log: list[dict] = []
        previous_failure_key: str | None = None
        previous_changed: set[str] | None = None

        lap_is_validation_failure = (
            worker_status == WorkerOutcomeStatus.validation_failed.value
            and bool(changed_files)
        )

        saved_revert = revert_on_failure
        revert_on_failure = False  # Don't revert during repair loop

        while lap_is_validation_failure and repair_attempt < max_repairs:
            repair_attempt += 1

            # Gather failure details from validation_results
            vr_list = list(getattr(lap_result, 'validation_results', []))
            validation_errors = [e for e in worker_errors if "validation" in e.lower() or "Validation command failed" in e]

            # Build failure fingerprint
            failure_parts = []
            for vr in vr_list:
                failure_parts.append(f"{vr.get('command','')}:{vr.get('exit_code','')}")
            current_failure_key = "|".join(failure_parts) if failure_parts else (
                "|".join(validation_errors) if validation_errors else str(repair_attempt)
            )

            # Stuck detection: same failure + same files
            current_changed_set = set(changed_files) if changed_files else set()
            if (previous_failure_key is not None
                and current_failure_key == previous_failure_key
                and current_changed_set == (previous_changed or set())):
                repair_attempts_log.append({
                    "attempt": repair_attempt,
                    "stuck": True,
                    "failure_key": current_failure_key,
                    "changed_files": list(changed_files),
                    "validation_results": vr_list,
                    "errors": validation_errors,
                })
                break

            previous_failure_key = current_failure_key
            previous_changed = current_changed_set

            # Emit progress to Drone Activity card
            self.statusChanged.emit(f"repairing {repair_attempt}/{max_repairs}")
            self.contentDelta.emit(f"**Validation failed.** Starting repair attempt {repair_attempt}/{max_repairs}...")

            # Build repair prompt
            repair_want_lines = [
                "## Validation failure — repair needed",
                "",
                "The previous lap made changes but validation failed. Fix the issue below. Run the same validation command again after fixing to confirm.",
                "",
            ]
            if changed_files:
                repair_want_lines.append("### Changed files from previous lap:")
                for f in changed_files:
                    repair_want_lines.append(f"- {f}")
                repair_want_lines.append("")

            # Show up to 3 validation failures
            for vr in vr_list[:3]:
                repair_want_lines.append("### Validation failure")
                repair_want_lines.append(f"Command: `{vr.get('command', '?')}`")
                repair_want_lines.append(f"Exit code: {vr.get('exit_code', '?')}")
                stdout_tail = (vr.get("stdout") or "")[-2000:]
                stderr_tail = (vr.get("stderr") or "")[-2000:]
                if stdout_tail.strip():
                    repair_want_lines.append(f"stdout tail:\n```\n{stdout_tail}\n```")
                if stderr_tail.strip():
                    repair_want_lines.append(f"stderr tail:\n```\n{stderr_tail}\n```")
                repair_want_lines.append("")

            for err in validation_errors[:2]:
                repair_want_lines.append(f"- {err}")
            if failure_parts:
                repair_want_lines.append(f"\nFailure fingerprint: {current_failure_key}")
            repair_want_lines.append("")
            repair_want_lines.append("Do not change unrelated files. Do not add new features. Fix only the validation failure.")

            repair_want = "\n".join(repair_want_lines)

            # Snapshot before repair lap
            repair_pre_sha = snapshot(workspace_root)

            # Record attempt before running
            repair_attempts_log.append({
                "attempt": repair_attempt,
                "failure_key": current_failure_key,
                "changed_files_before": list(changed_files),
                "validation_results": vr_list,
                "errors": validation_errors,
            })

            lap_result = self._harness_bridge.run_one_lap(repair_want)

            # Re-detect outcomes for loop check
            has_work, repair_changed = changes_since(workspace_root, repair_pre_sha)
            changed_files = list(lap_result.changed_files)
            worker_ok = lap_result.worker_ok
            worker_status = lap_result.worker_status
            worker_errors = list(lap_result.worker_errors)
            self._run_owned_files.update(changed_files)

            terminal_statuses = {
                WorkerOutcomeStatus.needs_followup.value,
                WorkerOutcomeStatus.scope_mismatch.value,
                WorkerOutcomeStatus.needs_planner_resolution.value,
                WorkerOutcomeStatus.harness_error.value,
                WorkerOutcomeStatus.edit_mechanics_blocked.value,
            }
            if worker_status in terminal_statuses:
                repair_attempts_log[-1]["worker_stopped"] = True
                repair_attempts_log[-1]["worker_status"] = worker_status
                break

            lap_is_validation_failure = (
                worker_status == WorkerOutcomeStatus.validation_failed.value
                or (
                    not worker_ok
                    and worker_errors
                    and any("Validation command failed" in e for e in worker_errors)
                )
            )

            if not lap_is_validation_failure:
                break  # Validation passed!

        # Restore original revert setting
        revert_on_failure = saved_revert

        # Compute cumulative changed files since pre_sha
        has_work_final, all_changed = changes_since(workspace_root, pre_sha)
        changed_files = list(all_changed)
        # --- End repair loop ---

        # 6. Determine if lap failed based on worker outcome
        hard_failure_statuses = {
            WorkerOutcomeStatus.validation_failed.value,
            WorkerOutcomeStatus.edit_mechanics_blocked.value,
            WorkerOutcomeStatus.harness_error.value,
            WorkerOutcomeStatus.needs_followup.value,
            WorkerOutcomeStatus.needs_planner_resolution.value,
            WorkerOutcomeStatus.craft_blocked.value,
            WorkerOutcomeStatus.craft_rejected.value,
            WorkerOutcomeStatus.scope_mismatch.value,
            WorkerOutcomeStatus.approval_rejected.value,
        }
        lap_failed = not worker_ok or worker_status in hard_failure_statuses or bool(worker_errors)

        if not lap_failed and not changed_files:
            # Empty lap -- not a failure, but no work done
            pass

        # 7. Check policy constraints
        policy_violations: list[str] = []

        if max_changed_files > 0 and len(changed_files) > max_changed_files:
            policy_violations.append(
                f"Changed {len(changed_files)} files, exceeds max_changed_files={max_changed_files}"
            )
            lap_failed = True

        for f in changed_files:
            if _path_matches_protected(f, protected_paths):
                policy_violations.append(
                    f"Changed protected path '{f}'"
                )
                lap_failed = True

        # --- Launch gate: verify the app boots ---
        if launch_command and not lap_failed and changed_files:
            self.contentDelta.emit("Verifying app launches…")
            executor = SandboxExecutor(workspace_root)
            result = executor.run_and_watch(
                launch_command,
                window_seconds=launch_window_seconds,
            )
            if result.survived_window:
                self.contentDelta.emit("App launched successfully.")
            else:
                output_tail = (result.output or "")[-300:]
                violation = (
                    f"App failed to launch after lap (exit {result.exit_code}): {output_tail}"
                )
                policy_violations.append(violation)
                lap_failed = True
                self.contentDelta.emit(
                    "App launch failed — lap will be reverted."
                )

        # 8. Revert on failure if policy says so
        rollback_status: str | None = None
        if lap_failed and revert_on_failure:
            if changed_files:
                revert_ok, revert_msg = _revert(pre_sha)
                if revert_ok:
                    clean_untracked_paths(workspace_root, changed_files)
                rollback_status = "reverted" if revert_ok else f"rollback_failed: {revert_msg}"
                if not revert_ok:
                    rollback_status = f"rollback_failed: {revert_msg}"
            else:
                rollback_status = "no_changes_to_revert"

        # 8.5. Commit successful lap changes
        commit_sha: str | None = None
        if not lap_failed and changed_files:
            commit_summary = lap_result.summary if hasattr(lap_result, 'summary') else ""
            self.contentDelta.emit("Validation passed, committing…")
            commit_ok, sha, commit_msg = commit_all(workspace_root, commit_summary)
            commit_sha = sha
            if not commit_ok:
                # Commit failure is not a lap failure — the edit is valid, just uncommitted
                summary_extra = f" [commit failed: {commit_msg}]"
            else:
                summary_extra = ""
        else:
            summary_extra = ""

        final_status: str
        if not lap_failed:
            final_status = "completed"
        elif rollback_status == "reverted" or rollback_status == "no_changes_to_revert":
            final_status = "failed"
        elif rollback_status and rollback_status.startswith("rollback_failed"):
            final_status = "failed"
        else:
            final_status = "failed"

        # 9. Build summary
        if lap_failed:
            violations_str = "; ".join(policy_violations) if policy_violations else ""
            errors_str = "; ".join(worker_errors) if worker_errors else ""
            parts = [s for s in [violations_str, errors_str] if s]
            combined = " | ".join(parts)
            summary = f"Harness-lap failed: {combined}" if combined else "Harness-lap failed."
            if rollback_status and "rollback_failed" in rollback_status:
                summary += " Rollback failed -- tree may be dirty."
            # Prepend repair info if repair was attempted
            if repair_attempt > 0:
                summary = f"Failed after {repair_attempt} repair attempt(s). " + summary
        else:
            summary = lap_result.summary + summary_extra
            if repair_attempt > 0:
                summary = f"Repaired on attempt {repair_attempt}. " + summary

        artifact: dict[str, Any] = {
            "has_work": bool(changed_files),
            "changed_files": changed_files,
            "worker_ok": worker_ok,
            "worker_status": worker_status,
            "worker_errors": worker_errors,
            "policy_violations": policy_violations,
            "rollback_status": rollback_status,
            "dirty_files_owned": list(self._run_owned_files),
            "commit_sha": commit_sha,
            "repair_attempts": repair_attempts_log,
            "attempted_target": self._lap_target,
        }

        receipt = DroneReceipt(
            run_id=self._run.run_id,
            drone_id=self._drone.id,
            drone_name=self._drone.name,
            status=final_status,
            started_at=dt.datetime.fromtimestamp(
                self._run.started_at, tz=dt.timezone.utc
            ).isoformat(),
            ended_at=dt.datetime.now(tz=dt.timezone.utc).isoformat(),
            summary=summary,
            produced_artifact=artifact,
            errors=worker_errors if lap_failed else [],
            elapsed_seconds=self._run.elapsed_seconds,
        )
        RunHistoryStore.save_run(workspace_root, receipt)
        self.contentDelta.emit(summary)
        self._run.mark(final_status)
        self.statusChanged.emit(final_status)
        self.receiptReady.emit(receipt)
    @property
    def run_state(self) -> DroneRun:
        return self._run

    @Slot()
    def run(self) -> None:
        logger.info("Drone run started: %s (%s)", self._drone.name, self._run.run_id)
        self._run.mark("running")
        self.statusChanged.emit("running")
        try:
            if self._is_harness_lap_drone():
                self._run_harness_lap()
                return
            if not is_folder_backed_drone(self._drone):
                raise ValueError("Only folder-backed command Drones with json-stdio protocol can be executed")

            goal = self._drone.description or self._drone.instructions
            result = run_folder_drone_sync(
                self._workspace_root,
                self._drone.id,
                self._drone,
                goal,
                run=self._run,
            )
            summary = str(result.get("summary") or "")
            if summary:
                self.contentDelta.emit(summary)
            receipt_data = result.get("receipt")
            receipt = (
                DroneReceipt.from_dict(receipt_data)
                if isinstance(receipt_data, dict)
                else None
            )
            if receipt is None:
                raise RuntimeError("Folder Drone did not return a receipt")
            self._run.mark(str(result.get("status") or receipt.status))
            self.statusChanged.emit(self._run.status)
            self.receiptReady.emit(receipt)
        except Exception as exc:
            logger.exception("Drone runner error")
            self._run.mark("failed")
            self.statusChanged.emit("failed")
            self.apiError.emit(-1, str(exc))
            self.receiptReady.emit(self._failed_receipt(str(exc)))
        finally:
            self.finished.emit()

    def set_approval_result(
        self,
        decision: ApprovalDecision,
        approval_id: str | None = None,
    ) -> None:
        _ = (decision, approval_id)

    def _failed_receipt(self, error: str) -> DroneReceipt:
        ended = dt.datetime.now(dt.timezone.utc).isoformat()
        return DroneReceipt(
            run_id=self._run.run_id,
            drone_id=self._drone.id,
            drone_name=self._drone.name,
            status="failed",
            started_at=dt.datetime.fromtimestamp(
                self._run.started_at,
                tz=dt.timezone.utc,
            ).isoformat(),
            ended_at=ended,
            tool_calls_made=0,
            tool_errors=0,
            summary="",
            output_contract=self._drone.output_contract,
            tool_calls=[],
            errors=[error],
            elapsed_seconds=self._run.elapsed_seconds,
            met=False,
            evidence="Folder-backed Drone execution failed.",
        )
