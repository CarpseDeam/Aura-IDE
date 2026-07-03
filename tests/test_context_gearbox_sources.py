from pathlib import Path

from aura.context_gearbox import sources
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import build_context_text


def _target_file_source():
    return next(source for source in sources.CONTEXT_SOURCES if source.source_id == "target_file_contents")


def _collect_target_file_text(
    workspace_root: Path,
    target_files: tuple[str, ...],
    role: RuntimeRole = RuntimeRole.WORKER,
):
    return sources.collect_source_text(
        _target_file_source(),
        role,
        workspace_root,
        target_files=target_files,
    )


def test_target_file_contents_loads_existing_file_contents(tmp_path: Path) -> None:
    target = tmp_path / "aura" / "feature.py"
    target.parent.mkdir()
    target.write_text("VALUE = 42\n", encoding="utf-8")

    text, entry, extra = _collect_target_file_text(tmp_path, ("aura/feature.py",))

    assert extra == []
    assert entry.included is True
    assert entry.char_count == len(text)
    assert entry.error is None
    assert "### Target file: aura/feature.py" in text
    assert "VALUE = 42" in text


def test_loaded_target_files_reports_only_preloadable_files(tmp_path: Path) -> None:
    target = tmp_path / "aura" / "feature.py"
    target.parent.mkdir()
    target.write_text("VALUE = 42\n", encoding="utf-8")

    loaded = sources.loaded_target_files(
        tmp_path,
        ("aura/feature.py", "missing.py", "../outside.py"),
    )

    assert loaded == ["aura/feature.py"]


def test_target_file_contents_skips_nonexistent_files_without_error(tmp_path: Path) -> None:
    target = tmp_path / "existing.py"
    target.write_text("print('ok')\n", encoding="utf-8")

    text, entry, _extra = _collect_target_file_text(
        tmp_path,
        ("missing.py", "existing.py"),
    )

    assert entry.included is True
    assert entry.error is None
    assert "### Target file: existing.py" in text
    assert "missing.py" not in text


def test_target_file_contents_truncates_file_over_per_file_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sources, "_TARGET_FILE_CHAR_CAP", 5)
    monkeypatch.setattr(sources, "_TARGET_FILES_TOTAL_CAP", 100)
    target = tmp_path / "large.py"
    target.write_text("abcdefghij", encoding="utf-8")

    text, entry, _extra = _collect_target_file_text(tmp_path, ("large.py",))

    assert entry.included is True
    assert "abcde" in text
    assert "fghij" not in text
    assert sources._TARGET_FILE_TRUNCATION_MARKER in text


def test_target_file_contents_uses_longer_fence_when_contents_include_backticks(tmp_path: Path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("before\n```\ninside\n```\nafter\n", encoding="utf-8")

    text, entry, _extra = _collect_target_file_text(tmp_path, ("notes.md",))

    assert entry.included is True
    assert "### Target file: notes.md\n````\n" in text
    assert "\nafter\n````" in text


def test_target_file_contents_halts_at_total_cap_and_names_omitted_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sources, "_TARGET_FILE_CHAR_CAP", 100)
    monkeypatch.setattr(sources, "_TARGET_FILES_TOTAL_CAP", 8)
    (tmp_path / "one.py").write_text("abcdef", encoding="utf-8")
    (tmp_path / "two.py").write_text("ghijkl", encoding="utf-8")
    (tmp_path / "three.py").write_text("mnopqr", encoding="utf-8")

    text, entry, _extra = _collect_target_file_text(
        tmp_path,
        ("one.py", "two.py", "three.py"),
    )

    assert entry.included is True
    assert "### Target file: one.py" in text
    assert "### Target file: two.py" in text
    assert "### Target file: three.py" not in text
    assert "target file contents total cap reached" in text
    assert "three.py" in text


def test_target_file_contents_is_worker_context_not_planner_context(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("SENTINEL = 'worker only'\n", encoding="utf-8")

    worker = build_context_text(RuntimeRole.WORKER, tmp_path, target_files=("app.py",))
    planner = build_context_text(RuntimeRole.PLANNER, tmp_path, target_files=("app.py",))

    assert "### Target file: app.py" in worker.context_text
    assert "SENTINEL = 'worker only'" in worker.context_text
    assert "### Target file: app.py" not in planner.context_text
    assert "SENTINEL = 'worker only'" not in planner.context_text

    worker_entry = next(entry for entry in worker.ledger if entry.source_id == "target_file_contents")
    planner_entry = next(entry for entry in planner.ledger if entry.source_id == "target_file_contents")
    assert worker_entry.included is True
    assert worker_entry.char_count > 0
    assert planner_entry.included is False
    assert planner_entry.reason == "not scoped to planner role"


def test_target_file_contents_unreadable_file_skips_without_ledger_error(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "unreadable.py"
    target.write_text("SECRET = True\n", encoding="utf-8")
    target_resolved = target.resolve()
    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):
        if self.resolve() == target_resolved:
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    text, entry, _extra = _collect_target_file_text(tmp_path, ("unreadable.py",))

    assert text == ""
    assert entry.included is False
    assert entry.reason == "no readable target files"
    assert entry.error is None


def test_code_quality_contract_replaces_existing_patterns_rule() -> None:
    contract = sources.CODE_QUALITY_CONTRACT

    assert "existing patterns" not in contract
    assert "leave the code better-shaped than you found it" in contract
    assert "Do not extend god files" in contract
