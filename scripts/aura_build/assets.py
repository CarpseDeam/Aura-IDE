"""Asset and post-build preparation: dist normalization, bundling, and validation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.aura_build.config import (
    BUILTIN_DRONES_DEST_REL,
    BUILTIN_DRONES_SOURCE_REL,
    DRONES_DEST_REL,
    DRONES_SOURCE_REL,
    FINAL_DIST_NAME,
    FINAL_EXE_NAME,
    OUTPUT_DIR,
    RAW_SOURCE_PACKAGES,
    SUPPORTED_GRAMMARS,
)

_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".pytest_cache", "*.pyc", "*.pyo",
    "*.tmp", "*.swp", "*~", "*.bak",
)


# Dist discovery and normalization


def find_created_dist_dir(root: Path) -> Path:
    """Find the dist folder Nuitka created."""
    build_dir = root / OUTPUT_DIR
    candidates = sorted(build_dir.glob("*.dist"), key=lambda p: p.stat().st_mtime, reverse=True)
    for dist_dir in candidates:
        if (dist_dir / FINAL_EXE_NAME).exists():
            return dist_dir
    print("Could not find dist folder with executable.")
    sys.exit(1)


def normalize_dist_dir(root: Path, created_dist_dir: Path) -> Path:
    """Ensure final dist folder is build/Aura.dist."""
    final_dist_dir = root / OUTPUT_DIR / FINAL_DIST_NAME
    if created_dist_dir.resolve() != final_dist_dir.resolve():
        if final_dist_dir.exists():
            shutil.rmtree(final_dist_dir, ignore_errors=True)
        created_dist_dir.rename(final_dist_dir)
    return final_dist_dir


def directory_size(path: Path) -> int:
    """Return the total size in bytes of every file under ``path``."""
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def format_size(num_bytes: int | None) -> str:
    """Render a byte count as a human-readable size."""
    if num_bytes is None:
        return "n/a"
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} GiB"


# Raw-source package copies


def _venv_package_path(python_exe: Path, package: str) -> Path | None:
    """Locate an installed package inside the build venv."""
    try:
        out = subprocess.check_output(
            [str(python_exe), "-c", f"import {package}; print({package}.__file__)"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return None
    return Path(out).resolve().parent


def copy_raw_source_packages(final_dist_dir: Path, python_exe: Path) -> None:
    """Copy packages Nuitka cannot compile into the dist as plain Python source.

    google-genai and charset_normalizer hang or crash Nuitka; click crashes the
    C compiler. All three are excluded via --nofollow-import-to and shipped as
    raw source instead.
    """
    for package, dest_rel in RAW_SOURCE_PACKAGES:
        source = _venv_package_path(python_exe, package)
        if source is None or not source.exists():
            print(f"Warning: {package} is not installed in the clean environment, skipping manual bundle.")
            continue
        target = final_dist_dir / dest_rel
        print(f"Bundling {package} as raw source: {source} -> {target}")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


# Drone bundles


def bundle_drones(root: Path, final_dist_dir: Path) -> None:
    """Copy bundled drone definitions from repo .aura/drones into the dist folder."""
    source = root / DRONES_SOURCE_REL
    dest = final_dist_dir / DRONES_DEST_REL

    if not source.exists():
        print("Repo .aura/drones not found; skipping drone bundle.")
        return

    # Remove any stale destination so leftover bundled drones don't persist
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    excluded_dirs = {"runs", "logs", "__pycache__", ".pytest_cache"}

    bundled = []
    for entry in sorted(source.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in excluded_dirs:
            continue
        if entry.name.startswith("."):
            continue
        drone_json = entry / "drone.json"
        if not drone_json.exists():
            continue

        shutil.copytree(entry, dest / entry.name, ignore=_COPY_IGNORE)
        bundled.append(entry.name)

    if bundled:
        print(f"Bundled {len(bundled)} drone(s): {', '.join(bundled)}")
    else:
        print("No drones found to bundle.")


def bundle_builtin_drones(root: Path, final_dist_dir: Path) -> None:
    """Copy built-in drone definitions from aura/drones/bundled into the dist.

    DroneStore._bundled_drones_root() loads from this path at runtime.
    """
    source = root / BUILTIN_DRONES_SOURCE_REL
    dest = final_dist_dir / BUILTIN_DRONES_DEST_REL

    if not source.exists():
        print("aura/drones/bundled not found; skipping built-in drone bundle.")
        return

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    bundled = []
    for entry in sorted(source.iterdir()):
        if not entry.is_dir():
            continue
        drone_json = entry / "drone.json"
        if not drone_json.exists():
            continue
        shutil.copytree(entry, dest / entry.name, ignore=_COPY_IGNORE)
        bundled.append(entry.name)

    if bundled:
        print(f"Bundled {len(bundled)} built-in drone(s): {', '.join(bundled)}")
    else:
        print("No built-in drones found to bundle.")


# Tree-sitter grammars


def grammar_prewarm_script() -> str:
    """Return the Python snippet used to pre-download tree-sitter grammars."""
    return """
import sys
import tree_sitter_language_pack as _lp

cache_path = sys.argv[1]
languages = sys.argv[2:]

print(f"Requested languages: {languages}")

try:
    try:
        _lp.configure(_lp.PackConfig(cache_dir=cache_path))
    except AttributeError:
        _lp.configure({"cache_dir": cache_path})
    print(f"Cache directory: {_lp.cache_dir()}")

    for lang in languages:
        print(f"Loading {lang}...")
        _lp.get_language(lang)

    print(f"Downloaded languages: {_lp.downloaded_languages()}")

    import pathlib
    cache_path_obj = pathlib.Path(cache_path)
    for p in cache_path_obj.rglob("*"):
        print(p)
except Exception as e:
    print(f"Grammar prewarm failed: {e}", file=sys.stderr)
    sys.exit(1)
""".strip()


def prewarm_grammars(final_dist_dir: Path, python_exe: Path) -> None:
    """Pre-download tree-sitter grammar .so files into the dist so runtime loading works."""
    grammar_dir = final_dist_dir / "grammars"
    grammar_dir.mkdir(parents=True, exist_ok=True)

    print("Pre-warming tree-sitter grammars...")
    try:
        output = subprocess.check_output(
            [str(python_exe), "-c", grammar_prewarm_script(), str(grammar_dir)] + SUPPORTED_GRAMMARS,
            stderr=subprocess.STDOUT,
        ).decode()
        print(output)
    except subprocess.CalledProcessError as exc:
        print(f"Grammar prewarm failed:\n{exc.output.decode()}")
        raise SystemExit("Tree-sitter grammar prewarm failed; aborting release build.") from exc

    # Verify the grammar directory is non-empty
    entries = list(grammar_dir.rglob("*"))
    if not entries:
        raise SystemExit("Grammar prewarm produced no files; aborting build.")

    print(f"Grammar prewarm complete: {len(entries)} file(s) in {grammar_dir}")


# Chromium / Playwright


def bundle_chromium(final_dist_dir: Path, python_exe: Path) -> None:
    """Install Chromium browser into the dist using Playwright's CLI."""
    browsers_dir = final_dist_dir / "ms-playwright"
    browsers_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)

    print("Installing Chromium for bundled Playwright...")
    try:
        subprocess.run(
            [str(python_exe), "-m", "playwright", "install", "chromium"],
            env=env, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Chromium install failed:\n{exc.stdout}\n{exc.stderr}")
        raise SystemExit("Chromium bundle failed; aborting build.") from exc

    entries = list(browsers_dir.rglob("*"))
    if not entries:
        raise SystemExit("Chromium install produced no files; aborting build.")
    print(f"Chromium bundled: {len(entries)} file(s) in {browsers_dir}")


def validate_playwright_bundle(final_dist_dir: Path, python_exe: Path) -> None:
    """Validate Playwright is properly bundled in the dist.

    Checks:
    1. Bundled Chromium exists at ms-playwright/chromium-*
    2. import playwright + playwright.sync_api + greenlet + pyee work
    """
    browsers_dir = final_dist_dir / "ms-playwright"

    # Check 1: Verify bundled Chromium
    if not browsers_dir.exists():
        raise SystemExit(
            "Playwright bundle validation FAILED: ms-playwright directory not found.\n"
            "Chromium was not bundled. Run bundle_chromium() before validation."
        )
    chromium_dirs = list(browsers_dir.glob("chromium-*"))
    if not chromium_dirs:
        raise SystemExit(
            "Playwright bundle validation FAILED: no chromium-* directory in ms-playwright.\n"
            "Chromium install produced no browser entry."
        )
    print(f"Playwright bundle: Chromium found ({chromium_dirs[0].name})")

    # Check 2: Verify playwright + greenlet + pyee are importable from the build venv
    print("Validating Playwright imports...")
    import_script = (
        "import sys; "
        "import playwright; print('playwright:', playwright.__file__); "
        "import playwright.sync_api; print('sync_api: OK'); "
        "import greenlet; print('greenlet:', greenlet.__file__); "
        "import pyee; print('pyee:', pyee.__file__)"
    )
    try:
        result = subprocess.run(
            [str(python_exe), "-c", import_script],
            check=True, capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.strip().splitlines():
            print(f"  {line}")
    except subprocess.CalledProcessError as exc:
        print(f"Playwright import validation FAILED:\n{exc.stdout}\n{exc.stderr}")
        raise SystemExit("Playwright bundle validation failed: imports broken.") from exc

    print("Playwright bundle validation: OK")
