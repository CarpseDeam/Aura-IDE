"""Tests for WorkArtifact domain model."""

import pytest

from aura.work_artifact.model import (
    WorkArtifact,
    WorkArtifactItem,
    WorkArtifactReceipt,
    WorkItemStatus,
)


class TestWorkArtifactReceipt:
    def test_defaults(self) -> None:
        receipt = WorkArtifactReceipt(status="ok")
        assert receipt.status == "ok"
        assert receipt.summary == ""
        assert receipt.modified_files == []
        assert receipt.errors == []

    def test_serializes_and_deserializes(self) -> None:
        receipt = WorkArtifactReceipt(
            status="ok",
            summary="Updated the parser",
            modified_files=["src/parser.py"],
            validation_summary="All tests passed",
            errors=[],
            result_status="completed",
        )
        d = receipt.to_dict()
        restored = WorkArtifactReceipt.from_dict(d)
        assert restored.status == "ok"
        assert restored.summary == "Updated the parser"
        assert restored.modified_files == ["src/parser.py"]


class TestWorkArtifactItem:
    def test_default_status_is_pending(self) -> None:
        item = WorkArtifactItem(
            id="item-1",
            title="Fix parser",
            intent="Repair the CSV parser",
            target_files=["src/parser.py"],
            acceptance="CSV parsing works",
        )
        assert item.status == WorkItemStatus.pending
        assert item.receipt is None

    def test_serializes_and_deserializes(self) -> None:
        receipt = WorkArtifactReceipt(status="ok", summary="Done")
        item = WorkArtifactItem(
            id="item-1",
            title="Fix parser",
            intent="Repair the CSV parser",
            target_files=["src/parser.py"],
            acceptance="CSV parsing works",
            status=WorkItemStatus.done,
            receipt=receipt,
        )
        d = item.to_dict()
        restored = WorkArtifactItem.from_dict(d)
        assert restored.id == "item-1"
        assert restored.title == "Fix parser"
        assert restored.status == WorkItemStatus.done
        assert restored.receipt is not None
        assert restored.receipt.status == "ok"

    def test_accepts_files_alias(self) -> None:
        """from_dict should accept both 'target_files' and 'files' keys."""
        raw = {"id": "i1", "title": "T", "intent": "I", "files": ["a.py"], "acceptance": "A"}
        item = WorkArtifactItem.from_dict(raw)
        assert item.target_files == ["a.py"]


class TestWorkArtifact:
    def test_validates_unique_item_ids(self) -> None:
        with pytest.raises(ValueError, match="Duplicate work item id"):
            WorkArtifact(
                artifact_id="art-1",
                goal="Test",
                work_items=[
                    WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
                    WorkArtifactItem(id="item-1", title="B", intent="I2", target_files=["b.py"], acceptance="A2"),
                ],
            )

    def test_validates_current_item_id_exists(self) -> None:
        with pytest.raises(ValueError, match="current_item_id.*does not match"):
            WorkArtifact(
                artifact_id="art-1",
                goal="Test",
                work_items=[
                    WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
                ],
                current_item_id="nonexistent",
            )

    def test_validates_required_fields(self) -> None:
        with pytest.raises(ValueError, match="must have"):
            WorkArtifact(
                artifact_id="art-1",
                goal="Test",
                work_items=[
                    WorkArtifactItem(id="item-1", title="", intent="", target_files=[], acceptance=""),
                ],
            )

    def test_serializes_and_deserializes(self) -> None:
        artifact = WorkArtifact(
            artifact_id="art-1",
            goal="Implement feature X",
            constraints=["No new deps"],
            allowed_files=["src/"],
            work_items=[
                WorkArtifactItem(id="item-1", title="Step 1", intent="Do A", target_files=["a.py"], acceptance="A works"),
            ],
            current_item_id="item-1",
        )
        d = artifact.to_dict()
        restored = WorkArtifact.from_dict(d)
        assert restored.artifact_id == "art-1"
        assert restored.goal == "Implement feature X"
        assert len(restored.work_items) == 1
        assert restored.current_item_id == "item-1"

    def test_selects_current_item(self) -> None:
        artifact = WorkArtifact(
            artifact_id="art-1",
            goal="Test",
            work_items=[
                WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
                WorkArtifactItem(id="item-2", title="B", intent="I2", target_files=["b.py"], acceptance="A2"),
            ],
            current_item_id="item-1",
        )
        item = artifact.current_item()
        assert item is not None
        assert item.id == "item-1"

    def test_returns_none_when_no_current_item(self) -> None:
        artifact = WorkArtifact(
            artifact_id="art-1",
            goal="Test",
            work_items=[
                WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
            ],
            current_item_id="",
        )
        assert artifact.current_item() is None

    def test_advances_to_next_pending_item_for_review(self) -> None:
        artifact = WorkArtifact(
            artifact_id="art-1",
            goal="Test",
            work_items=[
                WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
                WorkArtifactItem(id="item-2", title="B", intent="I2", target_files=["b.py"], acceptance="A2"),
            ],
            current_item_id="item-1",
        )
        next_item = artifact.advance()
        assert next_item is not None
        assert next_item.id == "item-2"
        assert artifact.current_item_id == "item-2"

    def test_work_item_status_has_exactly_three_members(self) -> None:
        """WorkItemStatus values are only pending, active, done."""
        values = {s.value for s in WorkItemStatus}
        assert values == {"pending", "active", "done"}

    def test_attach_done_receipt_is_audit_record(self) -> None:
        artifact = WorkArtifact(
            artifact_id="art-1",
            goal="Test",
            work_items=[
                WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
            ],
            current_item_id="item-1",
        )
        artifact.mark_done("item-1")
        receipt = WorkArtifactReceipt(status="ok", summary="Done")
        artifact.attach_receipt("item-1", receipt)
        item = artifact.current_item()
        assert item is not None
        assert item.status == WorkItemStatus.done  # set by mark_done, not receipt
        assert item.receipt is not None
        assert item.receipt.status == "ok"

    def test_attach_continuing_receipt_does_not_mutate_status(self) -> None:
        artifact = WorkArtifact(
            artifact_id="art-1",
            goal="Test",
            work_items=[
                WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
            ],
            current_item_id="item-1",
        )
        artifact.mark_active("item-1")
        receipt = WorkArtifactReceipt(status="continuing", summary="Worker needs a follow-up pass")
        artifact.attach_receipt("item-1", receipt)
        item = artifact.current_item()
        assert item is not None
        assert item.status == WorkItemStatus.active  # unchanged by receipt
        assert item.receipt is not None
        assert item.receipt.status == "continuing"

    def test_attach_failed_receipt_does_not_mutate_status(self) -> None:
        artifact = WorkArtifact(
            artifact_id="art-1",
            goal="Test",
            work_items=[
                WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
            ],
            current_item_id="item-1",
        )
        receipt = WorkArtifactReceipt(status="failed", summary="Something broke")
        artifact.attach_receipt("item-1", receipt)
        item = artifact.current_item()
        assert item is not None
        assert item.status == WorkItemStatus.pending  # unchanged by receipt
        assert item.receipt is not None
        assert item.receipt.status == "failed"

    def test_attach_cancelled_receipt_does_not_mutate_status(self) -> None:
        artifact = WorkArtifact(
            artifact_id="art-1",
            goal="Test",
            work_items=[
                WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
            ],
            current_item_id="item-1",
        )
        receipt = WorkArtifactReceipt(status="cancelled", summary="Cancelled")
        artifact.attach_receipt("item-1", receipt)
        item = artifact.current_item()
        assert item is not None
        assert item.status == WorkItemStatus.pending  # unchanged by receipt
        assert item.receipt is not None
        assert item.receipt.status == "cancelled"

    def test_attach_mismatch_receipt_does_not_mutate_status(self) -> None:
        artifact = WorkArtifact(
            artifact_id="art-1",
            goal="Test",
            work_items=[
                WorkArtifactItem(id="item-1", title="A", intent="I1", target_files=["a.py"], acceptance="A1"),
            ],
            current_item_id="item-1",
        )
        receipt = WorkArtifactReceipt(status="mismatch", summary="Tool mismatch")
        artifact.attach_receipt("item-1", receipt)
        item = artifact.current_item()
        assert item is not None
        assert item.status == WorkItemStatus.pending  # unchanged by receipt
        assert item.receipt is not None
        assert item.receipt.status == "mismatch"

    def test_from_dict_legacy_blocked_status_becomes_pending(self) -> None:
        """Old serialized 'blocked' status deserializes as pending."""
        raw = {
            "id": "item-1",
            "title": "Item",
            "intent": "Do it",
            "target_files": ["a.py"],
            "acceptance": "OK",
            "status": "blocked",
        }
        item = WorkArtifactItem.from_dict(raw)
        assert item.status == WorkItemStatus.pending
