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
from aura.drones.browse.monitor import apply_monitor_to_artifact
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
        "login_session": bool(permissions.get("login_session", False)),
        "login_wait_seconds": int(permissions.get("login_wait_seconds", 900)),
        # Monitor fingerprint settings
        "monitor_key": permissions.get("monitor_key") or None,
        "monitor_enabled": bool(permissions.get("monitor_enabled", False)),
        "monitor_fields": permissions.get("monitor_fields", ["title", "url", "body_excerpt"]),
        "monitor_excerpt_chars": int(permissions.get("monitor_excerpt_chars", 2000)),
    }


def _run_login_session_mode(
    *,
    workspace_root: Path,
    run: DroneRun,
    drone: DroneDefinition,
    start_url: str,
    browser_profile: str | None,
    login_wait_seconds: int,
    on_content: Callable[[str], None],
    on_status: Callable[[str], None],
    on_receipt: Callable[[DroneReceipt], None],
) -> None:
    """Run a visible login session and produce a receipt.

    Delegates to ``login_session.run_login_session`` for the browser
    lifecycle, then builds and saves a receipt from the result.
    """
    from aura.drones.browse.login_session import run_login_session
    from aura.drones.browse.artifacts import build_login_session_artifact

    on_content("Starting visible login session\u2026")

    if not browser_profile:
        _emit_failed_login(run, drone, start_url,
                           "browser_profile is required for login session",
                           workspace_root, on_content, on_status, on_receipt)
        return

    started = dt.datetime.now(dt.timezone.utc)

    action_trace: list[dict[str, Any]] = [
        {
            "type": "open_login_session",
            "url": start_url,
            "browser_profile": browser_profile,
        },
    ]

    result = run_login_session(
        start_url=start_url,
        browser_profile=browser_profile,
        wait_seconds=login_wait_seconds,
        on_content=on_content,
        cancel_event=run.cancel_event,
    )

    status = result["status"]
    final_url = result.get("final_url", "")
    page_title = result.get("title", "")
    errors: list[str] = result.get("errors", [])
    elapsed = result.get("elapsed_seconds", 0.0)

    success = status not in ("login_session_failed",)
    action_trace[0]["success"] = success

    artifact = build_login_session_artifact(
        start_url=start_url,
        final_url=final_url,
        page_title=page_title,
        status=status,
        browser_profile=browser_profile,
        action_trace=action_trace,
        errors=errors,
        skipped_reason=(
            f"Timed out after {login_wait_seconds}s"
            if status == "login_session_timeout"
            else (errors[0] if errors else None)
        ),
    )

    if status == "login_session_failed":
        summary = f"Login session failed for profile '{browser_profile}'"
        summary += f": {errors[0]}" if errors else ""
        on_content(summary)
        _save_login_receipt(run, drone, summary, artifact, errors, elapsed,
                            started, "failed", workspace_root, on_receipt)
        run.mark("failed")
        on_status("failed")
        return

    if status == "login_session_timeout":
        summary = f"Login session timed out for profile '{browser_profile}'."
        on_content(summary)
        _save_login_receipt(run, drone, summary, artifact, errors, elapsed,
                            started, "completed", workspace_root, on_receipt)
        run.mark("completed")
        on_status("completed")
        return

    if status == "login_session_cancelled":
        summary = f"Login session cancelled for profile '{browser_profile}'."
        on_content(summary)
        _save_login_receipt(run, drone, summary, artifact, errors, elapsed,
                            started, "cancelled", workspace_root, on_receipt)
        run.mark("cancelled")
        on_status("cancelled")
        return

    # login_session_closed
    summary = f"Login session closed for profile '{browser_profile}'."
    on_content(summary)
    _save_login_receipt(run, drone, summary, artifact, errors, elapsed,
                        started, "completed", workspace_root, on_receipt)
    run.mark("completed")
    on_status("completed")


def _save_login_receipt(
    run: DroneRun,
    drone: DroneDefinition,
    summary: str,
    produced_artifact: dict,
    errors: list[str],
    elapsed: float,
    started: dt.datetime,
    receipt_status: str,
    workspace_root: Path,
    on_receipt: Callable[[DroneReceipt], None],
) -> None:
    """Build, save, and emit a login session receipt."""
    ended = dt.datetime.now(dt.timezone.utc).isoformat()
    receipt = DroneReceipt(
        run_id=run.run_id,
        drone_id=drone.id,
        drone_name=drone.name,
        status=receipt_status,
        started_at=started.isoformat(),
        ended_at=ended,
        summary=summary,
        produced_artifact=produced_artifact,
        errors=errors,
        elapsed_seconds=elapsed,
    )
    on_receipt(receipt)
    RunHistoryStore.save_run(workspace_root, receipt)


def _emit_failed_login(
    run: DroneRun,
    drone: DroneDefinition,
    start_url: str,
    msg: str,
    workspace_root: Path,
    on_content: Callable[[str], None],
    on_status: Callable[[str], None],
    on_receipt: Callable[[DroneReceipt], None],
) -> None:
    """Emit a failed receipt when a login session cannot start."""
    from aura.drones.browse.artifacts import build_login_session_artifact

    on_content(msg)
    artifact = build_login_session_artifact(
        start_url=start_url,
        final_url="",
        page_title="",
        status="login_session_failed",
        browser_profile="",
        action_trace=[],
        errors=[msg],
        skipped_reason=msg,
    )
    started = dt.datetime.fromtimestamp(run.started_at, tz=dt.timezone.utc)
    _save_login_receipt(run, drone, msg, artifact, [msg], 0.0,
                        started, "failed", workspace_root, on_receipt)
    run.mark("failed")
    on_status("failed")


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

    login_session_flag: bool = settings["login_session"]
    login_wait_seconds: int = settings["login_wait_seconds"]

    # Monitor settings
    monitor_key: str | None = settings["monitor_key"]
    monitor_enabled: bool = settings["monitor_enabled"]
    monitor_fields: list[str] = settings["monitor_fields"]
    monitor_excerpt_chars: int = settings["monitor_excerpt_chars"]

    # --- Login session mode ---
    if login_session_flag:
        _run_login_session_mode(
            workspace_root=workspace_root,
            run=run,
            drone=drone,
            start_url=start_url,
            browser_profile=browser_profile,
            login_wait_seconds=login_wait_seconds,
            on_content=on_content,
            on_status=on_status,
            on_receipt=on_receipt,
        )
        return

    # --- Monitor config guard: require explicit target before browser launch ---
    if monitor_enabled:
        start_url_invalid = (
            not start_url
            or not start_url.strip()
            or start_url.strip() == "https://example.com"
        )
        monitor_key_invalid = not monitor_key or not monitor_key.strip()
        if start_url_invalid or monitor_key_invalid:
            summary = "Browse Monitor needs start_url and monitor_key before it can run."
            on_content(summary)
            produced_artifact = {
                "kind": "browse",
                "status": "skipped_monitor_config",
                "start_url": start_url,
                "final_url": "",
                "title": "",
                "action_trace": [],
                "before_snapshot": None,
                "after_snapshot": None,
                "candidate_count": 0,
                "skipped_reason": summary,
                "errors": [],
            }
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
                summary=summary,
                produced_artifact=produced_artifact,
                elapsed_seconds=run.elapsed_seconds,
            )
            on_receipt(receipt)
            RunHistoryStore.save_run(workspace_root, receipt)
            run.mark("completed")
            on_status("completed")
            return

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
            "persistent_session": bool(browser_profile),
        }
        profile_metadata["attempted_routes"] = runtime.route_metadata.get("attempted_routes", [])
        receipt = build_failed_receipt(
            run=run,
            drone=drone,
            start_url=start_url,
            summary=f"Browser unavailable: {runtime.unavailable_reason}",
            errors=[runtime.unavailable_reason],
            profile_metadata=profile_metadata,
        )
        if monitor_enabled and monitor_key:
            verdict = apply_monitor_to_artifact(
                receipt.produced_artifact,
                workspace_root=workspace_root,
                monitor_key=monitor_key,
                monitor_fields=monitor_fields,
                monitor_excerpt_chars=monitor_excerpt_chars,
                snapshot=None,
            )
            if verdict:
                receipt.summary += f" [monitor: {verdict}]"
        on_receipt(receipt)
        RunHistoryStore.save_run(workspace_root, receipt)
        run.mark("failed")
        on_status("failed")
        return

    _browser_meta_success = {}
    try:
        # Capture browser route metadata for artifact enrichment
        _browser_meta = runtime.route_metadata
        _browser_meta_success = {
            "browser_id": _browser_meta.get("browser_id", ""),
            "browser_label": _browser_meta.get("browser_label", ""),
            "browser_source": _browser_meta.get("browser_source", ""),
            "browser_persistent": _browser_meta.get("browser_persistent", False),
            "browser_visible": _browser_meta.get("browser_visible", False),
        }
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
            page.url,
            page.title(),
            login_required_text,
        ):
            on_content("Login required for this profile/session \u2014 skipping actions.")
            profile_metadata = {
                "browser_profile": browser_profile if browser_profile else None,
                "visible": visible,
                "persistent_session": bool(browser_profile),
            }
            profile_metadata.update(_browser_meta_success)
            produced_artifact = build_needs_login_artifact(
                start_url=start_url,
                final_url=page.url,
                page_title=page.title(),
                before_snapshot=before_snapshot,
                action_trace=action_trace,
                browser_profile=browser_profile,
                visible=visible,
                profile_metadata=profile_metadata,
            )
            if monitor_enabled and monitor_key:
                apply_monitor_to_artifact(
                    produced_artifact,
                    workspace_root=workspace_root,
                    monitor_key=monitor_key,
                    monitor_fields=monitor_fields,
                    monitor_excerpt_chars=monitor_excerpt_chars,
                    snapshot=before_snapshot,
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
            if monitor_enabled and monitor_key:
                verdict = produced_artifact.get("monitor", {}).get("verdict", "")
                if verdict:
                    receipt.summary += f" [monitor: {verdict}]"
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
            "persistent_session": bool(browser_profile),
        }
        profile_metadata.update(_browser_meta_success)

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

        # --- Monitor fingerprint ---
        if monitor_enabled and monitor_key:
            if policy_block is not None:
                snap_for_monitor = before_snapshot
            else:
                snap_for_monitor = after_snapshot if after_snapshot is not None else before_snapshot
            apply_monitor_to_artifact(
                produced_artifact,
                workspace_root=workspace_root,
                monitor_key=monitor_key,
                monitor_fields=monitor_fields,
                monitor_excerpt_chars=monitor_excerpt_chars,
                snapshot=snap_for_monitor,
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
        if monitor_enabled and monitor_key:
            verdict = produced_artifact.get("monitor", {}).get("verdict", "")
            if verdict:
                receipt.summary += f" [monitor: {verdict}]"
        on_receipt(receipt)
        RunHistoryStore.save_run(workspace_root, receipt)
        run.mark("completed")
        on_status("completed")

    except Exception as exc:
        logger.exception("Browse drone failed")
        profile_metadata = {
            "browser_profile": browser_profile if browser_profile else None,
            "visible": visible,
            "persistent_session": bool(browser_profile),
        }
        profile_metadata.update(_browser_meta_success)
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
        if monitor_enabled and monitor_key:
            verdict = apply_monitor_to_artifact(
                receipt.produced_artifact,
                workspace_root=workspace_root,
                monitor_key=monitor_key,
                monitor_fields=monitor_fields,
                monitor_excerpt_chars=monitor_excerpt_chars,
                snapshot=None,
            )
            if verdict:
                receipt.summary += f" [monitor: {verdict}]"
        on_receipt(receipt)
        RunHistoryStore.save_run(workspace_root, receipt)
        run.mark("failed")
        on_status("failed")
    finally:
        runtime.close()
