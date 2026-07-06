"""Work Artifact domain model.

A WorkArtifact is one visible approved job structure. A WorkArtifactItem
is one bounded internal execution unit. The user does not advance item-by-item
in the normal path. Aura executes item-sized requests internally under one
approved job.
"""
from __future__ import annotations

import copy
import enum
import time
from dataclasses import dataclass, field
from typing import Any


class WorkItemStatus(enum.Enum):
    """Status of a single work item in the artifact."""

    pending = "pending"
    active = "active"
    done = "done"


@dataclass
class WorkArtifactReceipt:
    """Result receipt attached to a WorkArtifactItem."""

    status: str
    summary: str = ""
    modified_files: list[str] = field(default_factory=list)
    validation_summary: str = ""
    errors: list[str] = field(default_factory=list)
    mismatch: dict[str, Any] | None = None
    result_status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "summary": self.summary,
            "modified_files": list(self.modified_files),
            "validation_summary": self.validation_summary,
            "result_status": self.result_status,
        }
        if self.errors:
            payload["errors"] = list(self.errors)
        if self.mismatch is not None:
            payload["mismatch"] = dict(self.mismatch)
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> WorkArtifactReceipt:
        if not isinstance(raw, dict):
            return cls(status="unknown")
        errors = raw.get("errors") if isinstance(raw.get("errors"), list) else []
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        return cls(
            status=str(raw.get("status", "")),
            summary=str(raw.get("summary", "")),
            modified_files=[str(f) for f in (raw.get("modified_files") or [])],
            validation_summary=str(raw.get("validation_summary", "")),
            errors=[str(e) for e in errors],
            mismatch=dict(raw["mismatch"]) if raw.get("mismatch") and isinstance(raw["mismatch"], dict) else None,
            result_status=str(raw.get("result_status", "")),
            metadata=dict(metadata),
        )


@dataclass
class WorkArtifactItem:
    """One bounded, reviewable, executable work item."""

    id: str
    title: str
    intent: str
    target_files: list[str] = field(default_factory=list)
    acceptance: str = ""
    status: WorkItemStatus = WorkItemStatus.pending
    receipt: WorkArtifactReceipt | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "intent": self.intent,
            "target_files": list(self.target_files),
            "acceptance": self.acceptance,
            "status": self.status.value,
        }
        if self.receipt is not None:
            payload["receipt"] = self.receipt.to_dict()
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> WorkArtifactItem:
        if not isinstance(raw, dict):
            raw = {}
        receipt_raw = raw.get("receipt")
        receipt = WorkArtifactReceipt.from_dict(receipt_raw) if receipt_raw else None
        raw_status = str(raw.get("status", "pending"))
        try:
            status = WorkItemStatus(raw_status)
        except ValueError:
            status = WorkItemStatus.pending
        return cls(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            intent=str(raw.get("intent", "")),
            target_files=[str(f) for f in (raw.get("target_files") or raw.get("files") or [])],
            acceptance=str(raw.get("acceptance", "")),
            status=status,
            receipt=receipt,
        )


@dataclass
class WorkArtifact:
    """One visible approved job structure.

    The Planner creates one WorkArtifact per multi-part task. The user
    approves the job once. Aura executes item runs internally under the
    same approval. Items are bounded internal execution units. The Worker
    executes one item-sized request at a time. The artifact advances to
    the next pending item for internal execution only — no per-item user
    review in the normal path.
    """

    artifact_id: str
    goal: str
    constraints: list[str] = field(default_factory=list)
    allowed_files: list[str] = field(default_factory=list)
    work_items: list[WorkArtifactItem] = field(default_factory=list)
    current_item_id: str = ""
    final_receipt: WorkArtifactReceipt | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.created_at == 0.0:
            now = time.time()
            self.created_at = now
            self.updated_at = now
        self._validate()

    def _validate(self) -> None:
        """Validate artifact invariants."""
        seen: set[str] = set()
        for item in self.work_items:
            if not item.id:
                raise ValueError("Every work item must have a non-empty id.")
            if not item.title:
                raise ValueError("Every work item must have a title.")
            if not item.intent:
                raise ValueError("Every work item must have an intent.")
            if not item.target_files:
                raise ValueError(f"Work item '{item.id}' must have target_files.")
            if not item.acceptance:
                raise ValueError(f"Work item '{item.id}' must have acceptance.")
            if item.id in seen:
                raise ValueError(f"Duplicate work item id: {item.id}")
            seen.add(item.id)
        if self.current_item_id:
            if not any(item.id == self.current_item_id for item in self.work_items):
                raise ValueError(
                    f"current_item_id '{self.current_item_id}' does not match any work item."
                )

    # ── serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "goal": self.goal,
            "constraints": list(self.constraints),
            "allowed_files": list(self.allowed_files),
            "work_items": [item.to_dict() for item in self.work_items],
            "current_item_id": self.current_item_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.final_receipt is not None:
            payload["final_receipt"] = self.final_receipt.to_dict()
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> WorkArtifact:
        if not isinstance(raw, dict):
            raw = {}
        items_raw = raw.get("work_items") if isinstance(raw.get("work_items"), list) else []
        receipt_raw = raw.get("final_receipt")
        return cls(
            artifact_id=str(raw.get("artifact_id", "")),
            goal=str(raw.get("goal", "")),
            constraints=[str(c) for c in (raw.get("constraints") or [])],
            allowed_files=[str(f) for f in (raw.get("allowed_files") or [])],
            work_items=[WorkArtifactItem.from_dict(item) for item in items_raw],
            current_item_id=str(raw.get("current_item_id", "")),
            final_receipt=WorkArtifactReceipt.from_dict(receipt_raw) if receipt_raw else None,
            created_at=float(raw.get("created_at", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0)),
        )

    # ── query helpers ─────────────────────────────────────────────────────

    def current_item(self) -> WorkArtifactItem | None:
        """Return the current work item, or None if none is set."""
        if not self.current_item_id:
            return None
        for item in self.work_items:
            if item.id == self.current_item_id:
                return item
        return None

    def next_pending_item(self) -> WorkArtifactItem | None:
        """Return the next pending work item after the current one."""
        if not self.work_items:
            return None
        found_current = False
        if self.current_item_id:
            for item in self.work_items:
                if item.id == self.current_item_id:
                    found_current = True
                    continue
                if found_current and item.status == WorkItemStatus.pending:
                    return item
        # If no current_item_id, return first pending
        if not self.current_item_id:
            for item in self.work_items:
                if item.status == WorkItemStatus.pending:
                    return item
        return None

    # ── mutation helpers ──────────────────────────────────────────────────

    def mark_active(self, item_id: str) -> None:
        """Mark an item as active (being worked on)."""
        for item in self.work_items:
            if item.id == item_id:
                item.status = WorkItemStatus.active
                self.updated_at = time.time()
                return
        raise ValueError(f"Item '{item_id}' not found in artifact.")

    def attach_receipt(self, item_id: str, receipt: WorkArtifactReceipt) -> None:
        """Attach a receipt to an item and update its status.

        Work Artifact item states are only pending, active, done.
        ``ok`` receipts mark the item done.
        ``continuing`` receipts keep the item active.
        All other receipts (cancelled, failed, mismatch, interrupted, etc.)
        return the item to pending with the receipt attached so it remains
        re-dispatchable. Interruptions are a run-level concern, not an item state.
        """
        for item in self.work_items:
            if item.id == item_id:
                item.receipt = receipt
                if receipt.status == "ok":
                    item.status = WorkItemStatus.done
                elif receipt.status == "continuing":
                    item.status = WorkItemStatus.active
                else:
                    item.status = WorkItemStatus.pending
                self.updated_at = time.time()
                self.current_item_id = item_id
                return
        raise ValueError(f"Item '{item_id}' not found in artifact.")

    def advance(self) -> WorkArtifactItem | None:
        """Advance to the next pending item.

        This selects the next pending item as current for internal
        execution. It must NOT dispatch the Worker.
        """
        next_item = self.next_pending_item()
        if next_item is not None:
            self.current_item_id = next_item.id
            self.updated_at = time.time()
        return next_item
