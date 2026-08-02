"""Integration tests: route-derived task_kind reaches context source selection.

These tests use the real ``build_context_text`` path (not a stubbed
``compose_system_prompt``) so they prove that the applicability gates
in ``sources.py`` correctly recognize the lane vocabulary produced by
``_task_kind_from_route`` in the bridge:

* ``implementation`` → code-quality, validation-selection, receipt contracts
* ``validation``     → validation-selection + receipt, NOT code-quality / scoped packs
* ``web_research`` / ``research_then_worker`` → research-shaped
* ``None`` (chat)    → no coding contracts
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


def test_single_implementation_includes_coding_contracts(tmp_path: Path) -> None:
    """SINGLE implementation must receive code-quality, validation-selection, receipt."""
    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind="implementation",
    )

    ids = _ledger_ids(ctx)
    assert "code_quality_contract" in ids
    assert "validation_selection_contract" in ids
    assert "receipt_contract" in ids


def test_single_validation_includes_validation_and_receipt(tmp_path: Path) -> None:
    """SINGLE validation must receive validation-selection and receipt."""
    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind="validation",
    )

    ids = _ledger_ids(ctx)
    assert "validation_selection_contract" in ids
    assert "receipt_contract" in ids


def test_single_validation_excludes_code_quality(tmp_path: Path) -> None:
    """Validation-only turns must NOT load the code-quality contract."""
    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind="validation",
    )

    included = _ledger_ids(ctx)
    assert "code_quality_contract" not in included


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


def test_validation_with_target_files_excludes_code_quality(tmp_path: Path) -> None:
    """Naming target files must not reopen the coding contract on a validation turn.

    ``bool(target_files)`` is the fallback for un-routed turns; validation is
    routed, so it stays authoritative.
    """
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
    assert "code_quality_contract" not in included
    assert "validation_selection_contract" in included
    assert "receipt_contract" in included


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
    assert "code_quality_contract" not in included
    assert "validation_selection_contract" in included
    assert "receipt_contract" in included


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
    assert "code_quality_contract" in included


def test_web_research_remains_research_shaped(tmp_path: Path) -> None:
    """web_research must stay research-shaped (no coding contracts in SINGLE)."""
    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind="web_research",
    )

    included = _ledger_ids(ctx)
    assert "code_quality_contract" not in included
    assert "validation_selection_contract" not in included
    assert "receipt_contract" not in included


def test_research_then_worker_remains_research_shaped(tmp_path: Path) -> None:
    """research_then_worker must stay research-shaped."""
    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind="research_then_worker",
    )

    included = _ledger_ids(ctx)
    assert "code_quality_contract" not in included
    assert "validation_selection_contract" not in included
    assert "receipt_contract" not in included


def test_chat_excludes_coding_contracts(tmp_path: Path) -> None:
    """Chat (task_kind=None) must not load any coding contracts."""
    ctx = build_context_text(
        RuntimeRole.SINGLE,
        tmp_path,
        task_kind=None,
    )

    included = _ledger_ids(ctx)
    assert "code_quality_contract" not in included
    assert "validation_selection_contract" not in included
    assert "receipt_contract" not in included
