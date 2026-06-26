"""Visible login/reauth session for an Aura-owned browser profile.

The user logs in manually in a visible browser window.
Aura never types passwords, handles MFA, reads cookies, or inspects passwords.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import threading
from typing import Callable

from aura.browser.runtime import BrowserRuntime
from aura.drones.browse.profiles import ensure_profile_dir

logger = logging.getLogger(__name__)


def run_login_session(
    *,
    start_url: str,
    browser_profile: str,
    wait_seconds: int,
    on_content: Callable[[str], None],
    cancel_event: threading.Event | None = None,
) -> dict:
    """Open a visible browser session for manual login/reauth.

    Launches an Aura-owned persistent profile browser window. The user
    logs in manually. Aura waits until the window closes or *wait_seconds*
    elapses, then returns a result dict.

    Args:
        start_url: URL to navigate to initially.
        browser_profile: Named profile (resolved via ensure_profile_dir).
        wait_seconds: Maximum seconds to wait for the user to close the browser.
        on_content: Callback for user-facing status messages.
        cancel_event: Optional event to signal cancellation.

    Returns:
        dict with keys: status, browser_profile, persistent_session, visible,
        start_url, final_url, title, elapsed_seconds, waited_seconds, errors.
    """
    profile_path = ensure_profile_dir(browser_profile)
    runtime = BrowserRuntime(headless=False, user_data_dir=profile_path)

    errors: list[str] = []
    final_url = ""
    title = ""
    status = "login_session_failed"
    started = dt.datetime.now()

    try:
        if not runtime.start():
            reason = runtime.unavailable_reason or "Browser runtime failed to start"
            errors.append(reason)
            on_content(f"Failed to start browser session: {reason}")
            return _login_result(status, browser_profile, start_url, final_url,
                                 title, errors, started)

        page = runtime.context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")
        final_url = page.url
        title = page.title()

        on_content(
            f"Opened visible login session for profile '{browser_profile}'. "
            f"Log in manually, then close the browser window."
        )

        # Poll until pages are gone, context is closed, or timeout
        deadline = time.monotonic() + wait_seconds
        while True:
            if cancel_event and cancel_event.is_set():
                status = "login_session_cancelled"
                break
            if time.monotonic() >= deadline:
                status = "login_session_timeout"
                break

            try:
                pages = runtime.context.pages
                if not pages:
                    status = "login_session_closed"
                    break
            except Exception:
                status = "login_session_closed"
                break

            time.sleep(1.0)

        # Capture final URL/title if still possible (best effort)
        if status in ("login_session_closed", "login_session_timeout", "login_session_cancelled"):
            try:
                pages = runtime.context.pages
                if pages:
                    final_url = pages[0].url
                    title = pages[0].title()
            except Exception:
                logger.debug("Could not capture final page state after session close")

    except Exception as exc:
        logger.exception("Login session failed")
        errors.append(str(exc))
        status = "login_session_failed"
        on_content(f"Login session failed: {exc}")
    finally:
        runtime.close()

    return _login_result(status, browser_profile, start_url, final_url, title,
                         errors, started)


def _login_result(
    status: str,
    browser_profile: str,
    start_url: str,
    final_url: str,
    title: str,
    errors: list[str],
    started: dt.datetime,
) -> dict:
    """Build the standardised result dict."""
    elapsed = (dt.datetime.now() - started).total_seconds()
    return {
        "status": status,
        "browser_profile": browser_profile,
        "persistent_session": True,
        "visible": True,
        "start_url": start_url,
        "final_url": final_url,
        "title": title,
        "elapsed_seconds": elapsed,
        "waited_seconds": min(elapsed, 0.0),
        "errors": errors,
    }
