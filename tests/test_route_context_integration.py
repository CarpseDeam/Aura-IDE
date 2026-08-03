"""Integration tests: route-derived task_kind reaches context source selection.

These tests use the real ``build_context_text`` path (not a stubbed
``compose_system_prompt``) so they prove that the applicability gates
in ``sources.py`` correctly recognize the lane vocabulary produced by
``_task_kind_from_route`` in the bridge:

* ``implementation`` → scoped coding packs when the target files match
* ``validation``     → no scoped coding packs
* ``web_research`` / ``research_then_worker`` → research-shaped
* ``None`` (chat)    → nothing coding-shaped

The three generic coding contracts (code-quality, validation-selection,
receipt) are no longer part of this routing question for SINGLE: the compact
production capsule owns those rules outright, so they are Worker-only sources
and never vary by lane.  What remains lane-sensitive is the scoped packs.
"""

from pathlib import Path

from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import build_context_text


def _ledger_ids(context, *, included: bool = True) -> set[str]:
    return {
        entry.source_id
        for entry in context.ledger
        if entry.included is included
    }


_GENERIC_CONTRACTS = (
    "code_quality_contract",
    "validation_selection_contract",
    "receipt_contract",
)


def test_no_lane_reopens_the_generic_contracts_for_single(tmp_path: Path) -> None:
    """The capsule owns code shape, validation, and receipts on every lane.

    A lane that reintroduces one of these blocks gives that rule a second
    owner in the prompt, which is what the consolidation removed.
    """
    for task_kind in ("implementation", "validation", "bugfix", "refactor", None):
        ctx = build_context_text(
            RuntimeRole.SINGLE,
            tmp_path,
            task_kind=task_kind,
        )
        included = _ledger_ids(ctx)
        for contract in _GENERIC_CONTRACTS:
            assert contract not in included, f"{contract} came back on lane {task_kind!r}"


def test_worker_still_receives_the_generic_contracts(tmp_path: Path) -> None:
    """The legacy Worker path keeps the separately injected contracts."""
    ctx = build_context_text(
        RuntimeRole.WORKER,
        tmp_path,
        task_kind="implementation",
    )

    included = _ledger_ids(ctx)
    for contract in _GENERIC_CONTRACTS:
        assert contract in included


def test_validation_does_not_activate_scoped_coding_packs(tmp_path: Path) -> None:
    """Validation must not load unrelated scoped coding packs merely because
    the boolean gate was broadened."""
    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind="validation",
    )

    included = _ledger_ids(ctx)
    for pack_id in ("gui_rules", "drone_rules", "provider_rules", "build_pipeline_rules"):
        assert pack_id not in included, f"{pack_id} should not be loaded for validation"


def test_validation_with_target_files_stays_validation_shaped(tmp_path: Path) -> None:
    """Naming target files must not reopen coding context on a validation turn."""
    gui_file = tmp_path / "aura" / "gui" / "main_window.py"
    gui_file.parent.mkdir(parents=True, exist_ok=True)
    gui_file.write_text("x = 1\n", encoding="utf-8")

    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind="validation",
        target_files=("aura/gui/main_window.py",),
    )

    included = _ledger_ids(ctx)
    assert "gui_rules" not in included
    # The manifest is a fact about the turn, not coaching, so it survives.
    assert "target_file_contents" in included


def test_validation_with_matching_target_files_excludes_scoped_packs(
    tmp_path: Path,
) -> None:
    """Validating GUI/provider/build/drone files loads none of those packs."""
    relpaths = (
        "aura/gui/main_window.py",
        "aura/client/deepseek.py",
        "installer/build_installer.py",
        "aura/drones/runner.py",
    )
    for relpath in relpaths:
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")

    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind="validation",
        target_files=relpaths,
    )

    included = _ledger_ids(ctx)
    for pack_id in ("gui_rules", "drone_rules", "provider_rules", "build_pipeline_rules"):
        assert pack_id not in included, f"{pack_id} should not be loaded for validation"


def test_implementation_with_target_files_still_loads_scoped_packs(
    tmp_path: Path,
) -> None:
    """The validation gate must not suppress packs on ordinary implementation."""
    gui_file = tmp_path / "aura" / "gui" / "main_window.py"
    gui_file.parent.mkdir(parents=True, exist_ok=True)
    gui_file.write_text("x = 1\n", encoding="utf-8")

    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind="implementation",
        target_files=("aura/gui/main_window.py",),
    )

    included = _ledger_ids(ctx)
    assert "gui_rules" in included


def test_research_and_chat_lanes_load_no_scoped_coding_packs(tmp_path: Path) -> None:
    """Research and chat stay free of subsystem implementation guidance."""
    for task_kind in ("web_research", "research_then_worker", None):
        ctx = build_context_text(
            RuntimeRole.SINGLE,
            tmp_path,
            task_kind=task_kind,
        )
        included = _ledger_ids(ctx)
        for pack_id in ("gui_rules", "drone_rules", "provider_rules", "build_pipeline_rules"):
            assert pack_id not in included, f"{pack_id} loaded on lane {task_kind!r}"
