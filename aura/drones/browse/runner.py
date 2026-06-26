"""Real anonymous Browse Drone: navigate, snapshot, safe click/fill, receipt."""

from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path
from typing import Any, Callable

from aura.browser.runtime import BrowserRuntime
from aura.drones.browse.models import BrowseCandidate, BrowseSnapshot
from aura.drones.browse.policy import PolicyResult, classify_action
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
    }


def _extract_candidates(page, max_candidates: int = 30) -> list[BrowseCandidate]:
    """Discover interactive elements on the page via JS evaluation.

    Assigns ``data-aura-browse-id`` attributes on each candidate so that
    subsequent Playwright selectors can target them.
    """
    js_code = """
    (maxCandidates) => {
        const selectors = [
            'a[href]',
            'button',
            'input:not([type="hidden"])',
            'textarea',
            'select',
            '[role="button"]',
            '[role="link"]',
            '[role="textbox"]',
            '[role="combobox"]',
            '[role="checkbox"]',
            '[role="radio"]',
            '[role="searchbox"]',
            '[role="menuitem"]',
            '[role="option"]',
            '[role="tab"]',
            '[role="switch"]',
            '[onclick]',
            '[tabindex]:not([tabindex="-1"])'
        ];
        const all = document.querySelectorAll(selectors.join(','));
        const results = [];
        let count = 0;
        for (const el of all) {
            if (count >= maxCandidates) break;

            // Visibility check
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            if (
                style.display === 'none' ||
                style.visibility === 'hidden' ||
                parseFloat(style.opacity) === 0 ||
                rect.width === 0 ||
                rect.height === 0
            ) {
                continue;
            }

            const id = 'c' + count;
            el.setAttribute('data-aura-browse-id', id);

            const tag = el.tagName.toLowerCase();
            let role = el.getAttribute('role') || '';
            if (!role) {
                if (tag === 'a') {
                    role = 'link';
                } else if (tag === 'button') {
                    role = 'button';
                } else if (tag === 'textarea') {
                    role = 'textbox';
                } else if (tag === 'select') {
                    role = 'combobox';
                } else if (tag === 'input') {
                    const inputType = (el.getAttribute('type') || 'text').toLowerCase();
                    role = inputType === 'checkbox' ? 'checkbox' :
                           inputType === 'radio' ? 'radio' :
                           inputType === 'submit' || inputType === 'button' ? 'button' :
                           inputType === 'search' ? 'searchbox' :
                           'textbox';
                }
            }

            // Label resolution: aria-label > name > placeholder > title > textContent
            let label = el.getAttribute('aria-label') || '';
            if (!label) {
                label = el.getAttribute('name') || '';
            }
            if (!label) {
                label = el.getAttribute('placeholder') || '';
            }
            if (!label) {
                label = el.getAttribute('title') || '';
            }
            if (!label && el.textContent) {
                label = el.textContent.trim().substring(0, 100);
            }

            const enabled = !el.disabled;
            let href = '';
            if (tag === 'a') {
                href = el.getAttribute('href') || '';
            }
            let input_type = '';
            if (tag === 'input') {
                input_type = el.getAttribute('type') || 'text';
            } else if (tag === 'textarea') {
                input_type = 'textarea';
            }

            results.push({
                id: id,
                role: role,
                tag: tag,
                label: label,
                enabled: enabled,
                visible: true,
                href: href,
                input_type: input_type,
            });
            count++;
        }
        return results;
    }
    """
    try:
        raw_candidates = page.evaluate(js_code, max_candidates)
    except Exception:
        logger.exception("Failed to extract candidates via JS evaluation")
        return []

    candidates: list[BrowseCandidate] = []
    for rc in raw_candidates:
        candidates.append(
            BrowseCandidate(
                id=rc.get("id", ""),
                role=rc.get("role", ""),
                tag=rc.get("tag", ""),
                label=rc.get("label", ""),
                enabled=rc.get("enabled", True),
                visible=rc.get("visible", True),
                href=rc.get("href", ""),
                input_type=rc.get("input_type", ""),
            )
        )
    return candidates


def _capture_snapshot(page, max_candidates: int = 30) -> BrowseSnapshot:
    """Capture the current page state as a BrowseSnapshot."""
    url = page.url
    title = page.title()
    # Normalize body inner_text: collapse whitespace, strip, cap at 2000
    body_text = page.inner_text("body")
    body_text = re.sub(r"\s+", " ", body_text).strip()
    body_excerpt = body_text[:2000]
    candidates = _extract_candidates(page, max_candidates=max_candidates)
    candidate_dicts = [c.to_dict() for c in candidates]
    return BrowseSnapshot(
        url=url,
        title=title,
        body_excerpt=body_excerpt,
        candidates=candidate_dicts,
        candidate_count=len(candidates),
    )


def _find_candidate(
    candidates: list[BrowseCandidate], text: str
) -> BrowseCandidate | None:
    """Find the best visible, enabled candidate by substring label match.

    Uses case-insensitive substring matching.  Prefers exact match first,
    then shortest label among matches (most specific).
    """
    text_lower = text.lower()
    matches: list[BrowseCandidate] = []
    for c in candidates:
        if not c.visible or not c.enabled:
            continue
        if text_lower in c.label.lower():
            matches.append(c)
    if not matches:
        return None
    # Prefer exact match, then shortest label
    exact = [m for m in matches if m.label.lower() == text_lower]
    if exact:
        return min(exact, key=lambda m: len(m.label))
    return min(matches, key=lambda m: len(m.label))




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
    has_action = bool(click_text or fill_text)

    on_content(f"Navigating to {start_url}\u2026")

    runtime = BrowserRuntime(headless=True)
    if not runtime.start():
        _emit_failed_receipt(
            workspace_root=workspace_root,
            run=run,
            drone=drone,
            start_url=start_url,
            summary=f"Browser unavailable: {runtime.unavailable_reason}",
            errors=[runtime.unavailable_reason],
            on_receipt=on_receipt,
            on_status=on_status,
        )
        return

    try:
        runtime.context.set_default_navigation_timeout(15000)
        page = runtime.context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")

        # Snapshot #1 — before any action
        before_snapshot = _capture_snapshot(page, max_candidates=max_candidates)
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

        # --- Optional click ---
        if click_text:
            candidates = _extract_candidates(page, max_candidates=max_candidates)
            candidate = _find_candidate(candidates, click_text)

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
                    try:
                        page.click(f'[data-aura-browse-id="{candidate.id}"]')
                        page.wait_for_timeout(500)
                        try:
                            page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            logger.debug(
                                "wait_for_load_state timed out after click on %s",
                                candidate.id,
                            )
                        entry["success"] = True
                        action_executed = True
                    except Exception as exc:
                        entry["success"] = False
                        if not skipped_reason:
                            skipped_reason = str(exc)
                else:
                    entry["success"] = False
                    policy_block = (policy_result, candidate, "click")
                    if not skipped_reason:
                        skipped_reason = policy_result.reason
                action_trace.append(entry)

        # --- Optional fill ---
        if fill_text and fill_value and policy_block is None:
            candidates = _extract_candidates(page, max_candidates=max_candidates)
            candidate = _find_candidate(candidates, fill_text)

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
                    try:
                        page.fill(f'[data-aura-browse-id="{candidate.id}"]', fill_value)
                        entry["success"] = True
                        action_executed = True
                    except Exception as exc:
                        entry["success"] = False
                        if not skipped_reason:
                            skipped_reason = str(exc)
                else:
                    entry["success"] = False
                    policy_block = (policy_result, candidate, "fill")
                    if not skipped_reason:
                        skipped_reason = policy_result.reason
                action_trace.append(entry)

        # Receipt-building: branch on policy_block
        if policy_block is not None:
            policy_result, banned_candidate, banned_action_type = policy_block
            proposed_action = {
                "action_type": banned_action_type,
                "target_id": banned_candidate.id,
                "target_label": banned_candidate.label,
                "target_role": banned_candidate.role,
                "target_tag": banned_candidate.tag,
                "target_href": banned_candidate.href,
                "current_url": page.url,
                "page_title": page.title(),
                "reason": policy_result.reason,
                "matched_text": policy_result.matched_text,
            }
            artifact_status = "needs_planner_decision" if policy_result.verdict == "needs_planner_decision" else "blocked_manual"
            after_snapshot = None
        else:
            artifact_status = "completed"
            after_snapshot = _capture_snapshot(page, max_candidates=max_candidates) if (action_executed or has_action) else None

        ended = dt.datetime.now(dt.timezone.utc).isoformat()
        produced_artifact = {
            "kind": "browse",
            "status": artifact_status,
            "start_url": start_url,
            "final_url": page.url,
            "title": page.title(),
            "action_trace": action_trace,
            "before_snapshot": before_snapshot.to_dict(),
            "after_snapshot": after_snapshot.to_dict() if after_snapshot else None,
            "candidate_count": before_snapshot.candidate_count,
            "skipped_reason": skipped_reason,
            "errors": [],
        }
        if policy_block:
            produced_artifact["proposed_action"] = proposed_action

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
        _emit_failed_receipt(
            workspace_root=workspace_root,
            run=run,
            drone=drone,
            start_url=start_url,
            summary=f"Browse failed: {exc}",
            errors=[str(exc)],
            on_receipt=on_receipt,
            on_status=on_status,
            action_trace=[
                {"type": "navigate", "url": start_url, "success": False},
            ],
        )
    finally:
        runtime.close()


def _emit_failed_receipt(
    *,
    workspace_root: Path,
    run: DroneRun,
    drone: DroneDefinition,
    start_url: str,
    summary: str,
    errors: list[str],
    on_receipt: Callable[[DroneReceipt], None],
    on_status: Callable[[str], None],
    action_trace: list[dict] | None = None,
) -> None:
    """Build and emit a failed receipt, then mark the run as failed."""
    ended = dt.datetime.now(dt.timezone.utc).isoformat()
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
        produced_artifact={
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
        },
        errors=errors,
        elapsed_seconds=run.elapsed_seconds,
    )
    on_receipt(receipt)
    RunHistoryStore.save_run(workspace_root, receipt)
    run.mark("failed")
    on_status("failed")
