from __future__ import annotations

from types import SimpleNamespace

from aura.execution_checklist import (
    ExecutionChecklistItem,
    build_execution_checklist,
    build_execution_checklist_items,
)


def _step(
    step_id: str,
    title: str,
    *,
    files: list[str] | None = None,
    checklist_item_ids: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=step_id,
        title=title,
        goal=title,
        files=list(files or []),
        checklist_item_ids=list(checklist_item_ids or []),
    )


def _request(**kwargs) -> SimpleNamespace:
    defaults = {
        "goal": "Update the feature.",
        "summary": "Update feature",
        "spec": "",
        "acceptance": "",
        "files": ["src/a.py"],
        "steps": [],
        "todo_checklist": [],
        "risk_notes": [],
        "validation_commands": [],
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_explicit_checklist_preserves_order_and_row_count() -> None:
    req = _request(
        steps=[
            _step("step-1", "Create helper"),
            _step("step-2", "Wire caller"),
        ],
        todo_checklist=[
            {"id": "a", "description": "Create helper module", "owning_step_id": "step-1"},
            {"id": "b", "description": "Wire helper into caller", "owning_step_id": "step-2"},
            {"id": "c", "description": "Run validation", "owning_step_id": "step-2"},
        ],
    )

    items = build_execution_checklist_items(req)

    assert [item.id for item in items] == ["a", "b", "c"]
    assert [item.description for item in items] == [
        "Create helper module",
        "Wire helper into caller",
        "Run validation",
    ]


def test_accepted_work_contract_bullets_become_rows() -> None:
    req = _request(
        spec=(
            "Accepted work contract:\n"
            "- Create WorkerToolEventRouter\n"
            "- Move worker tool args routing\n"
            "- Wire WorkerEventHandler to the router\n"
        ),
        steps=[
            _step("step-1", "Create router"),
            _step("step-2", "Wire handler"),
        ],
    )

    items = build_execution_checklist_items(req)

    assert [item.description for item in items] == [
        "Create WorkerToolEventRouter",
        "Move worker tool args routing",
        "Wire WorkerEventHandler to the router",
    ]
    assert [item.owning_step_id for item in items] == ["step-1", "step-1", "step-2"]


def test_steps_fallback_creates_one_row_per_step() -> None:
    req = _request(
        steps=[
            _step("step-1", "Create helper module", files=["src/helper.py"]),
            _step("step-2", "Wire caller", files=["src/caller.py"]),
        ],
        files=["src/helper.py", "src/caller.py"],
    )

    items = build_execution_checklist_items(req)

    assert [item.id for item in items] == ["step-1", "step-2"]
    assert [item.description for item in items] == [
        "Create helper module",
        "Wire caller",
    ]
    assert [item.files for item in items] == [
        ("src/helper.py",),
        ("src/caller.py",),
    ]


def test_multi_step_work_never_collapses_to_one_row() -> None:
    req = _request(
        goal="Refactor shell pipeline helpers.",
        summary="Extract helpers and wire caller.",
        files=["src/helper.py", "src/caller.py"],
        steps=[
            _step("step-1", "Create helper module"),
            _step("step-2", "Wire caller"),
            _step("step-3", "Run validation"),
        ],
    )

    snapshot = build_execution_checklist(req)

    assert len(snapshot.items) == 3
    assert [item.id for item in snapshot.items] == ["step-1", "step-2", "step-3"]


def test_non_trivial_flat_work_does_not_collapse_to_one_row() -> None:
    req = _request(
        goal="Refactor the dispatch checklist subsystem.",
        summary="Refactor dispatch checklist subsystem",
        files=["src/bridge.py", "src/session.py"],
        spec="Refactor the checklist flow across bridge and session.",
        steps=[],
    )

    items = build_execution_checklist_items(req)

    assert items == []


def test_tiny_flat_task_can_produce_one_row() -> None:
    req = _request(
        goal="Fix a typo in the settings label.",
        summary="Fix settings label typo",
        files=["src/settings.py"],
        spec="Correct the visible typo only.",
        steps=[],
    )

    items = build_execution_checklist_items(req)

    assert len(items) == 1
    assert items[0].id == "step-1"
    assert items[0].description == "Fix settings label typo"
    assert items[0].owning_step_id == "step-1"


def test_owning_step_id_is_preserved() -> None:
    req = _request(
        steps=[
            _step("step-1", "Create helper"),
            _step("step-2", "Wire caller"),
        ],
        execution_checklist=[
            ExecutionChecklistItem(
                id="wire-caller",
                description="Wire caller",
                owning_step_id="step-2",
            )
        ],
    )

    items = build_execution_checklist_items(req)

    assert len(items) == 1
    assert items[0].id == "wire-caller"
    assert items[0].owning_step_id == "step-2"
