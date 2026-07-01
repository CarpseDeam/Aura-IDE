from __future__ import annotations

from pathlib import Path

from aura.conversation import WorkerDispatchRequest


def _validation_scratch_files(root: Path | None) -> set[Path]:
    if root is None:
        return set()

    files = set(_root_check_files(root))
    tmp_dir = root / ".aura" / "tmp"
    if tmp_dir.is_dir():
        for pattern in ("dump*.py", "_check*.py", "check*.py", "tmp*.py", "_tmp*.py", "_inspect*.py", "inspect*.py", "diagnostic*.py", "_diagnostic*.py"):
            files.update(path for path in tmp_dir.glob(pattern) if path.is_file())
    return files


def _cleanup_new_validation_scratch_files(root: Path, before: set[Path]) -> list[str]:
    cleaned: list[str] = []
    for path in _validation_scratch_files(root):
        if path in before:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        cleaned.append(rel)
    return sorted(cleaned)


def _root_check_files(root: Path | None) -> set[Path]:
    if root is None:
        return set()
    try:
        files: set[Path] = set()
        for pattern in ("_check*.py", "_tmp*.py", "tmp_*.py", "_inspect*.py", "inspect*.py", "diagnostic*.py", "_diagnostic*.py"):
            files.update(path.resolve() for path in root.glob(pattern) if path.is_file())
        return files
    except OSError:
        return set()


def _request_allows_root_check_files(req: WorkerDispatchRequest) -> bool:
    text = " ".join([req.goal, req.spec, req.acceptance, req.summary]).lower()
    if "_check" in text or "_tmp" in text or "tmp_" in text:
        return True
    if "_inspect" in text or "_diagnostic" in text:
        return True
    return any(
        Path(path).name.startswith(("_check", "_tmp", "tmp_", "_inspect", "inspect", "diagnostic", "_diagnostic"))
        for path in req.files
    )
