"""Artifact and receipt builders for browse drone."""

from __future__ import annotations

import datetime as dt
from typing import Any

from aura.drones.browse.models import BrowseCandidate, BrowseSnapshot
from aura.drones.browse.policy import PolicyResult
from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun


def detect_login_required(
    body_excerpt: str,
    candidates: list[dict],
    login_required_text: list[str],
) -> bool:
    """Check whether page content suggests a login wall.

    Checks the body excerpt and candidate labels (lowercased) against
    each phrase in ``login_required_text`` (also lowercased).
    Returns True if any match is found.
    """
    excerpt_lower = body_excerpt.lower()
    for phrase in login_required_text:
        if phrase.lower() in excerpt_lower:
            return True
    for candidate in candidates:
        label = candidate.get("label", "") if isinstance(candidate, dict) else getattr(candidate, "label", "")
        if label:
            label_lower = label.lower()
            for phrase in login_required_text:
                if phrase.lower() in label_lower:
                    return True
    return False


def build_needs_login_artifact(
    *,
    start_url: str,
    final_url: str,
    page_title: str,
    before_snapshot: BrowseSnapshot,
    action_trace: list[dict[str, Any]],
    browser_profile: str | None = None,
    visible: bool = False,
) -> dict[str, Any]:
    """Build a produced_artifact dict for a login-required page.

    The artifact status is ``needs_login`` with no after_snapshot.
    Profile metadata (browser_profile, persistent_session, visible)
    are included directly.
    """
    return {
        "kind": "browse",
        "status": "needs_login",
        "start_url": start_url,
        "final_url": final_url,
        "title": page_title,
        "action_trace": action_trace,
        "before_snapshot": before_snapshot.to_dict(),
        "after_snapshot": None,
        "candidate_count": before_snapshot.candidate_count,
        "skipped_reason": "Login required for this profile/session",
        "errors": [],
        "browser_profile": browser_profile,
        "persistent_session": browser_profile is not None,
        "visible": visible,
    }


def build_boundary_artifact(
    *,
    start_url: str,
    final_url: str,
    page_title: str,
    before_snapshot: BrowseSnapshot,
    action_trace: list[dict[str, Any]],
    policy_result: PolicyResult,
    candidate: BrowseCandidate,
    action_type: str,
    skipped_reason: str,
    profile_metadata: dict | None = None,
) -> dict[str, Any]:
    """Build a produced_artifact dict for a boundary (policy-blocked) action.

    The artifact status is ``needs_planner_decision`` when the policy verdict
    is ``needs_planner_decision``, otherwise ``blocked_manual``.

    If ``profile_metadata`` is provided, its fields (browser_profile,
    persistent_session, visible) are merged into the artifact.
    """
    proposed_action = {
        "action_type": action_type,
        "target_id": candidate.id,
        "target_label": candidate.label,
        "target_role": candidate.role,
        "target_tag": candidate.tag,
        "target_href": candidate.href,
        "current_url": final_url,
        "page_title": page_title,
        "reason": policy_result.reason,
        "matched_text": policy_result.matched_text,
    }
    artifact_status = (
        "needs_planner_decision"
        if policy_result.verdict == "needs_planner_decision"
        else "blocked_manual"
    )
    artifact = {
        "kind": "browse",
        "status": artifact_status,
        "start_url": start_url,
        "final_url": final_url,
        "title": page_title,
        "action_trace": action_trace,
        "before_snapshot": before_snapshot.to_dict(),
        "after_snapshot": None,
        "candidate_count": before_snapshot.candidate_count,
        "skipped_reason": skipped_reason,
        "errors": [],
        "proposed_action": proposed_action,
    }
    if profile_metadata:
        artifact.update(profile_metadata)
    return artifact


def build_completed_artifact(
    *,
    start_url: str,
    final_url: str,
    page_title: str,
    before_snapshot: BrowseSnapshot,
    after_snapshot: BrowseSnapshot | None,
    action_trace: list[dict[str, Any]],
    skipped_reason: str | None,
    profile_metadata: dict | None = None,
) -> dict[str, Any]:
    """Build a produced_artifact dict for a successfully completed browse run.

    If ``profile_metadata`` is provided, its fields (browser_profile,
    persistent_session, visible) are merged into the artifact.
    """
    artifact = {
        "kind": "browse",
        "status": "completed",
        "start_url": start_url,
        "final_url": final_url,
        "title": page_title,
        "action_trace": action_trace,
        "before_snapshot": before_snapshot.to_dict(),
        "after_snapshot": after_snapshot.to_dict() if after_snapshot else None,
        "candidate_count": before_snapshot.candidate_count,
        "skipped_reason": skipped_reason,
        "errors": [],
    }
    if profile_metadata:
        artifact.update(profile_metadata)
    return artifact


def build_failed_receipt(
    *,
    run: DroneRun,
    drone: DroneDefinition,
    start_url: str,
    summary: str,
    errors: list[str],
    action_trace: list[dict] | None = None,
    profile_metadata: dict | None = None,
) -> DroneReceipt:
    """Build a failed DroneReceipt without emitting signals or saving.

    The caller is responsible for calling ``on_receipt``, ``RunHistoryStore.save_run``,
    ``run.mark("failed")``, and ``on_status("failed")``.

    If ``profile_metadata`` is provided, its fields are merged into the
    produced_artifact dict.
    """
    ended = dt.datetime.now(dt.timezone.utc).isoformat()
    produced_artifact = {
        "kind": "browse",
        "status": "failed",
        "start_url": start_url,
        "final_url": "",
        "title": "",
        "action_trace": action_trace or [],
        "before_snapshot": None,
        "after_snapshot": None,
        "candidate_count": 0,
        "skipped_reason": None,
        "errors": errors,
    }
    if profile_metadata:
        produced_artifact.update(profile_metadata)
    receipt = DroneReceipt(
        run_id=run.run_id,
        drone_id=drone.id,
        drone_name=drone.name,
        status="failed",
        started_at=dt.datetime.fromtimestamp(
            run.started_at, tz=dt.timezone.utc
        ).isoformat(),
        ended_at=ended,
        summary=summary,
        produced_artifact=produced_artifact,
        errors=errors,
        elapsed_seconds=run.elapsed_seconds,
    )
    return receipt
