from pathlib import Path

from aura.context_gearbox import sources
from aura.context_gearbox.runtime import build_context_text


def _target_source():
    return next(
        source
        for source in sources.CONTEXT_SOURCES
        if source.source_id == "target_file_contents"
    )


def test_target_files_are_a_compact_manifest_for_the_one_production_context(
    tmp_path: Path,
) -> None:
    target = tmp_path / "aura" / "feature.py"
    target.parent.mkdir()
    target.write_text("VALUE = 42\n", encoding="utf-8")

    text, entry, extra = sources.collect_source_text(
        _target_source(), tmp_path, target_files=("aura/feature.py",)
    )

    assert extra == []
    assert entry.included is True
    assert entry.reason == "manifest of user-mentioned files; contents load through read tools"
    assert "aura/feature.py" in text
    assert "VALUE = 42" not in text
    assert "User-mentioned files" in text
    assert "edit surface" not in text.lower()
    assert "context anchors" in text.lower()


def test_context_ledger_has_no_runtime_role_dimension(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    composed = build_context_text(tmp_path, target_files=("app.py",))
    target_entry = next(
        entry for entry in composed.ledger if entry.source_id == "target_file_contents"
    )

    assert target_entry.included is True
    assert not hasattr(target_entry, "role")
    assert not hasattr(next(iter(sources.CONTEXT_SOURCES)), "roles")
