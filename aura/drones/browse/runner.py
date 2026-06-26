"""Real anonymous Browse Drone: navigate, snapshot, safe click/fill, receipt."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Callable

from aura.browser.runtime import BrowserRuntime
from aura.drones.browse.actions import click_candidate, fill_candidate
from aura.drones.browse.artifacts import (
    build_boundary_artifact,
    build_completed_artifact,
    build_failed_receipt,
    build_needs_login_artifact,
    detect_login_required,
)
from aura.drones.browse.models import BrowseCandidate
from aura.drones.browse.policy import PolicyResult, classify_action
from aura.drones.browse.profiles import ensure_profile_dir
from aura.drones.browse.snapshot import capture_snapshot, extract_candidates, find_candidate
from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun
from aura.drones.store import RunHistoryStore

logger = logging.getLogger(__name__)


def _read_browse_settings(permissions: dict) -> dict:
    """Extract browse settings from drone permissions with safe defaults."""
    return {
        "start_url": permissions.get("start_url", "https://example.com"),
        "click_text": permissions.get("click_text") or None,
        "fill_text": permissions.get("fill_text") or None,
        "fill_value": permissions.get("fill_value") or None,
        "max_candidates": int(permissions.get("max_candidates", 30)),
        "allowed_consequential_actions": permissions.get("allowed_consequential_actions", []),
        "browser_profile": permissions.get("browser_profile") or None,
        "visible": bool(permissions.get("visible", False)),
        "requires_login": bool(permissions.get("requires_login", False)),
        "login_required_text": permissions.get("login_required_text", ["log in", "sign in"]),
    }


def run_browse_drone(
    workspace_root: Path,
    run: DroneRun,
    drone: DroneDefinition,
    *,
    on_content: Callable[[str], None],
    on_status: Callable[[str], None],
    on_receipt: Callable[[DroneReceipt], None],
) -> None:
    """Run an anonymous browse drone: navigate, snapshot, optionally click/fill.

    Fail-soft everywhere: any Playwright/navigation/action failure produces a
    failed receipt without crashing.
    """
    settings = _read_browse_settings(drone.permissions)
    start_url = settings["start_url"]
    click_text = settings["click_text"]
    fill_text = settings["fill_text"]
    fill_value = settings["fill_value"]
    max_candidates = settings["max_candidates"]
    allowed_consequential_actions = settings["allowed_consequential_actions"]
    browser_profile: str | None = settings["browser_profile"]
    visible: bool = settings["visible"]
    requires_login: bool = settings["requires_login"]
    login_required_text: list[str] = settings["login_required_text"]
    has_action = bool(click_text or fill_text)

    on_content(f"Navigating to {start_url}\u2026")

    if browser_profile:
        profile_path = ensure_profile_dir(browser_profile)
        runtime = BrowserRuntime(headless=not visible, user_data_dir=profile_path)
    else:
        runtime = BrowserRuntime(headless=True)
    if not runtime.start():
        profile_metadata = {
            "browser_profile": browser_profile if browser_profile else None,
            "visible": visible,
        }
        receipt = build_failed_receipt(
            run=run,
            drone=drone,
            start_url=start_url,
            summary=f"Browser unavailable: {runtime.unavailable_reason}",
            errors=[runtime.unavailable_reason],
            profile_metadata=profile_metadata,
        )
        on_receipt(receipt)
        RunHistoryStore.save_run(workspace_root, receipt)
        run.mark("failed")
        on_status("failed")
        return

    try:
        runtime.context.set_default_navigation_timeout(15000)
        page = runtime.context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")

        # Snapshot #1 — before any action
        before_snapshot = capture_snapshot(page, max_candidates=max_candidates)
        on_content(
            f"Loaded {page.url} \u2014 {before_snapshot.candidate_count} candidates found."
        )
        on_status("captured")

        action_trace: list[dict[str, Any]] = [
            {"type": "navigate", "url": start_url, "success": True},
        ]
        skipped_reason: str | None = None
        policy_block: tuple[PolicyResult, BrowseCandidate, str] | None = None
        action_executed: bool = False

        # --- Login detection ---
        if requires_login and detect_login_required(
            before_snapshot.body_excerpt,
            before_snapshot.candidates,
            login_required_text,
        ):
            on_content("Login required for this profile/session \u2014 skipping actions.")
            profile_metadata = {
                "browser_profile": browser_profile if browser_profile else None,
                "visible": visible,
            }
            produced_artifact = build_needs_login_artifact(
                start_url=start_url,
                final_url=page.url,
                page_title=page.title(),
                before_snapshot=before_snapshot,
                action_trace=action_trace,
                browser_profile=browser_profile,
                visible=visible,
            )
            ended = dt.datetime.now(dt.timezone.utc).isoformat()
            receipt = DroneReceipt(
                run_id=run.run_id,
                drone_id=drone.id,
                drone_name=drone.name,
                status="completed",
                started_at=dt.datetime.fromtimestamp(
                    run.started_at, tz=dt.timezone.utc
                ).isoformat(),
                ended_at=ended,
                summary=f"Login needed for {page.url} with profile '{browser_profile}'" if browser_profile else f"Login needed for {page.url}",
                produced_artifact=produced_artifact,
                elapsed_seconds=run.elapsed_seconds,
            )
            on_receipt(receipt)
            RunHistoryStore.save_run(workspace_root, receipt)
            run.mark("completed")
            on_status("completed")
            return

        # --- Optional click ---
        if click_text:
            candidates = extract_candidates(page, max_candidates=max_candidates)
            candidate = find_candidate(candidates, click_text)

            if candidate is None:
                action_trace.append(
                    {
                        "type": "click",
                        "candidate_id": "",
                        "label": click_text,
                        "href": "",
                        "policy_result": None,
                        "policy_reason": "",
                        "matched_text": None,
                        "success": False,
                    }
                )
                if not skipped_reason:
                    skipped_reason = f"No matching visible enabled candidate for '{click_text}'"
            elif policy_block is None:
                policy_result = classify_action("click", candidate, page.url, page.title(), allowed_consequential_actions)
                entry = {
                    "type": "click",
                    "candidate_id": candidate.id,
                    "label": candidate.label,
                    "href": candidate.href,
                    "policy_result": policy_result.verdict,
                    "policy_reason": policy_result.reason,
                    "matched_text": policy_result.matched_text,
                }
                if policy_result.verdict == "allow":
                    success, error = click_candidate(page, candidate)
                    entry["success"] = success
                    if success:
                        action_executed = True
                    else:
                        if not skipped_reason:
                            skipped_reason = error
                else:
                    entry["success"] = False
                    policy_block = (policy_result, candidate, "click")
                    if not skipped_reason:
                        skipped_reason = policy_result.reason
                action_trace.append(entry)

        # --- Optional fill ---
        if fill_text and fill_value and policy_block is None:
            candidates = extract_candidates(page, max_candidates=max_candidates)
            candidate = find_candidate(candidates, fill_text)

            if candidate is None:
                action_trace.append(
                    {
                        "type": "fill",
                        "candidate_id": "",
                        "label": fill_text,
                        "href": "",
                        "policy_result": None,
                        "policy_reason": "",
                        "matched_text": None,
                        "value": fill_value,
                        "success": False,
                    }
                )
                if not skipped_reason:
                    skipped_reason = f"No matching visible enabled input for '{fill_text}'"
            else:
                policy_result = classify_action("fill", candidate, page.url, page.title(), allowed_consequential_actions)
                entry = {
                    "type": "fill",
                    "candidate_id": candidate.id,
                    "label": candidate.label,
                    "href": candidate.href,
                    "policy_result": policy_result.verdict,
                    "policy_reason": policy_result.reason,
                    "matched_text": policy_result.matched_text,
                    "value": fill_value,
                }
                if policy_result.verdict == "allow":
                    success, error = fill_candidate(page, candidate, fill_value)
                    entry["success"] = success
                    if success:
                        action_executed = True
                    else:
                        if not skipped_reason:
                            skipped_reason = error
                else:
                    entry["success"] = False
                    policy_block = (policy_result, candidate, "fill")
                    if not skipped_reason:
                        skipped_reason = policy_result.reason
                action_trace.append(entry)

        # Build profile_metadata for all non-login artifacts
        profile_metadata = {
            "browser_profile": browser_profile if browser_profile else None,
            "visible": visible,
        }

        # Receipt-building: branch on policy_block
        if policy_block is not None:
            policy_result, banned_candidate, banned_action_type = policy_block
            produced_artifact = build_boundary_artifact(
                start_url=start_url,
                final_url=page.url,
                page_title=page.title(),
                before_snapshot=before_snapshot,
                action_trace=action_trace,
                policy_result=policy_result,
                candidate=banned_candidate,
                action_type=banned_action_type,
                skipped_reason=skipped_reason or "",
                profile_metadata=profile_metadata,
            )
        else:
            after_snapshot = capture_snapshot(page, max_candidates=max_candidates) if (action_executed or has_action) else None
            produced_artifact = build_completed_artifact(
                start_url=start_url,
                final_url=page.url,
                page_title=page.title(),
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                action_trace=action_trace,
                skipped_reason=skipped_reason,
                profile_metadata=profile_metadata,
            )

        ended = dt.datetime.now(dt.timezone.utc).isoformat()
        receipt = DroneReceipt(
            run_id=run.run_id,
            drone_id=drone.id,
            drone_name=drone.name,
            status="completed",
            started_at=dt.datetime.fromtimestamp(
                run.started_at, tz=dt.timezone.utc
            ).isoformat(),
            ended_at=ended,
            summary=f"Browsed {page.url} \u2014 {before_snapshot.candidate_count} candidates",
            produced_artifact=produced_artifact,
            elapsed_seconds=run.elapsed_seconds,
        )
        on_receipt(receipt)
        RunHistoryStore.save_run(workspace_root, receipt)
        run.mark("completed")
        on_status("completed")

    except Exception as exc:
        logger.exception("Browse drone failed")
        profile_metadata = {
            "browser_profile": browser_profile if browser_profile else None,
            "visible": visible,
        }
        receipt = build_failed_receipt(
            run=run,
            drone=drone,
            start_url=start_url,
            summary=f"Browse failed: {exc}",
            errors=[str(exc)],
            action_trace=[
                {"type": "navigate", "url": start_url, "success": False},
            ],
            profile_metadata=profile_metadata,
        )
        on_receipt(receipt)
        RunHistoryStore.save_run(workspace_root, receipt)
        run.mark("failed")
        on_status("failed")
    finally:
        runtime.close()
