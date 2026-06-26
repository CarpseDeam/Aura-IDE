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
    page_url: str,
    page_title: str,
    login_required_text: list[str],
) -> bool:
    """Check whether page content suggests a login wall.

    Checks hardcoded strong login-wall phrases against body excerpt,
    page URL, and page title.  Then checks caller-provided phrases
    only against the body excerpt.  Does NOT examine candidate labels.
    """
    _ = candidates  # kept for signature compatibility

    excerpt_lower = body_excerpt.lower()
    url_lower = page_url.lower()
    title_lower = page_title.lower()

    # Hardcoded strong login-wall phrases
    strong_phrases = [
        "please log in",
        "please sign in",
        "sign in to continue",
        "login required",
        "you must be logged in",
        "authentication required",
        "session expired",
    ]
    for phrase in strong_phrases:
        if phrase in excerpt_lower or phrase in title_lower or phrase in url_lower:
            return True

    # Caller-provided phrases — body only
    for phrase in login_required_text:
        if phrase.lower() in excerpt_lower:
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


def build_login_session_artifact(
    *,
    start_url: str,
    final_url: str,
    page_title: str,
    status: str,
    browser_profile: str,
    action_trace: list[dict[str, Any]],
    errors: list[str],
    skipped_reason: str | None = None,
) -> dict[str, Any]:
    """Build a produced_artifact dict for a login session result.

    Status is one of the login session status values
    (e.g. ``login_session_closed``, ``login_session_timeout``,
    ``login_session_failed``).  Snapshots are None because
    no automated candidate extraction occurs in a login session.
    """
    return {
        "kind": "browse",
        "status": status,
        "start_url": start_url,
        "final_url": final_url,
        "title": page_title,
        "action_trace": action_trace,
        "before_snapshot": None,
        "after_snapshot": None,
        "candidate_count": 0,
        "skipped_reason": skipped_reason,
        "errors": errors,
        "browser_profile": browser_profile,
        "persistent_session": True,
        "visible": True,
    }
