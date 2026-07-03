"""Unified receipt and session-state types for the Aura Browser Service.

Every browser operation returns a ``BrowserReceipt``.  The service tracks its
own lifecycle in a ``BrowserSession``.  Page listings use ``PageInfo``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# BrowserReceipt — unified operation receipt
# ---------------------------------------------------------------------------


@dataclass
class BrowserReceipt:
    """Structured receipt returned from every browser operation.

    Backward-compatible with existing callers that construct receipts
    with the original fields (controller_version, browser_executable, …).
    """

    # -- existing fields (preserved) --------------------------------------
    controller_version: str = "1.0"
    browser_executable: str = ""
    browser_profile_dir: str = ""
    browser_pid: int | None = None
    cdp_url: str = ""
    requested_url: str = ""
    first_navigated_url: str = ""
    final_active_url: str = ""
    page_title: str = ""
    navigation_status: str = "not_started"
    browser_ready: bool = False
    phase_errors: dict[str, str] = field(default_factory=dict)

    # -- new unified-receipt fields --------------------------------------
    operation: str = ""
    phase: str = ""
    observation_status: str = "not_started"   # not_started | success | partial | failed
    action_status: str = "not_started"        # not_started | success | failed | not_implemented
    session_id: str = ""
    started_at: str = ""
    reused_existing: bool = False
    page_index: int = -1
    page_count: int = 0
    requested_target: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- computed ---------------------------------------------------------

    @property
    def ok(self) -> bool:
        """True when the operation completed without errors."""
        if self.phase_errors:
            return False
        if not self.browser_ready:
            return False
        if self.navigation_status == "failed":
            return False
        if self.observation_status == "failed":
            return False
        if self.action_status in ("failed", "not_implemented"):
            return False
        return True

    @property
    def navigation_ok(self) -> bool:
        """True specifically when navigation completed successfully."""
        return self.navigation_status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "controller_version": self.controller_version,
            "browser_executable": self.browser_executable,
            "browser_profile_dir": self.browser_profile_dir,
            "browser_pid": self.browser_pid,
            "cdp_url": self.cdp_url,
            "requested_url": self.requested_url,
            "first_navigated_url": self.first_navigated_url,
            "final_active_url": self.final_active_url,
            "page_title": self.page_title,
            "browser_ready": self.browser_ready,
            "navigation_status": self.navigation_status,
            "phase_errors": dict(self.phase_errors),
            "operation": self.operation,
            "phase": self.phase,
            "observation_status": self.observation_status,
            "action_status": self.action_status,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "reused_existing": self.reused_existing,
            "page_index": self.page_index,
            "page_count": self.page_count,
            "requested_target": self.requested_target,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# BrowserSession — lifecycle state
# ---------------------------------------------------------------------------


@dataclass
class BrowserSession:
    """Serializable snapshot of one browser-service lifecycle."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = field(default_factory=lambda: dt.datetime.now().astimezone().isoformat())
    browser_executable: str = ""
    browser_profile_dir: str = ""
    browser_pid: int | None = None
    cdp_url: str = ""
    connected: bool = False
    closed: bool = False
    page_count: int = 0
    active_page_index: int = -1
    active_page_url: str = ""
    active_page_title: str = ""
    last_operation: str = ""
    last_phase: str = ""
    last_error_phase: str = ""
    last_error: str = ""
    reused_existing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "browser_executable": self.browser_executable,
            "browser_profile_dir": self.browser_profile_dir,
            "browser_pid": self.browser_pid,
            "cdp_url": self.cdp_url,
            "connected": self.connected,
            "closed": self.closed,
            "page_count": self.page_count,
            "active_page_index": self.active_page_index,
            "active_page_url": self.active_page_url,
            "active_page_title": self.active_page_title,
            "last_operation": self.last_operation,
            "last_phase": self.last_phase,
            "last_error_phase": self.last_error_phase,
            "last_error": self.last_error,
            "reused_existing": self.reused_existing,
        }


# ---------------------------------------------------------------------------
# PageInfo — tab descriptor
# ---------------------------------------------------------------------------


@dataclass
class PageInfo:
    """Lightweight page/tab descriptor for ``list_pages()``."""

    index: int = -1
    url: str = ""
    title: str = ""
    is_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "url": self.url,
            "title": self.title,
            "is_active": self.is_active,
        }
