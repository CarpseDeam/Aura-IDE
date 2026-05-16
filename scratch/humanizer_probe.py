from pathlib import Path


def resolve_path(raw: str) -> Path:
    raw = raw.strip()
    return Path(raw).expanduser().resolve()


def existing_paths(raw_paths: list[str]) -> list[Path]:
    return [p for p in (resolve_path(r) for r in raw_paths) if p.exists()]


def split_files_and_dirs(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    files = [p for p in paths if p.is_file()]
    dirs = [p for p in paths if p.is_dir()]
    return files, dirs
