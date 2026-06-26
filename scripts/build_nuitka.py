"""Build script for Aura EXE using Nuitka."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Configuration

APP_NAME = "Aura"
PACKAGE_NAME = "aura"

ICON_PATH = "media/AurA.ico"
MEDIA_DIR = "media"
OUTPUT_DIR = "build"
DEFAULT_NUITKA_JOBS = max(1, (os.cpu_count() or 2) // 2)

FINAL_DIST_NAME = f"{APP_NAME}.dist"
FINAL_EXE_NAME = f"{APP_NAME}.exe"
ZIP_NAME = "Aura-Windows-x64.zip"
UPDATER_HELPER_SOURCE = Path(PACKAGE_NAME) / "windows_updater.cmd"
UPDATER_HELPER_DIST_NAME = "AuraUpdater.cmd"

DRONES_SOURCE_REL = Path(".aura") / "drones"
DRONES_DEST_REL = Path(".aura") / "drones"

BUILTIN_DRONES_SOURCE_REL = Path("aura") / "drones" / "bundled"
BUILTIN_DRONES_DEST_REL = Path("aura") / "drones" / "bundled"

SUPPORTED_GRAMMARS = [
    "javascript", "typescript", "tsx", "go", "rust",
    "java", "c", "cpp", "csharp", "php", "ruby", "swift",
    "kotlin", "dart", "scala", "lua", "bash", "powershell",
    "html", "css", "scss", "json", "yaml", "toml", "xml",
    "sql", "markdown", "dockerfile",
    "gdscript",
    "gdshader",
]

REQUIRED_MEDIA_FILES = [
    "account_tree_.svg",
    "arrow_forward_24dp.svg",
    "AurA.ico",
    "Aura-Working.mp4",
    "commit.svg",
    "diff-view.png",
    "dispatch.png",
    "file-change-dialog.png",
    "file_24.svg",
    "folder_24.svg",
    "fork_right.svg",
    "mermaid.min.js",
    "new_conv.svg",
    "open_conversation.svg",
    "plan_and_code.gif",
    "read_only.svg",
    "settings_24dp.svg",
    "token-cost.png",
    "workflow-complete.png",
    "working.png",
]

# Signing Configuration
SIGN_CERT = os.environ.get("AURA_SIGN_CERT")
SIGN_PASS = os.environ.get("AURA_SIGN_PASS")


# Installer Configuration
INSTALLER_ISS_PATH = "scripts/installer/Aura.iss"
INSTALLER_BASE_NAME = "AuraSetup"


# Version Management

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def normalize_version(version: str) -> str:
    """Normalize a version string and require X.Y.Z format."""
    clean_version = version.strip().lstrip("vV")
    if not VERSION_PATTERN.fullmatch(clean_version):
        raise SystemExit("Invalid version. Expected X.Y.Z or vX.Y.Z, for example 1.3.4.")
    return clean_version


def read_current_version(root: Path) -> str:
    """Read the current package version from aura/version.py."""
    version_file = root / "aura" / "version.py"
    content = version_file.read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', content)
    if not match:
        raise SystemExit(f"Could not find __version__ in {version_file}")
    return normalize_version(match.group(1))


def write_text_if_changed(path: Path, content: str) -> None:
    """Write text only when the resulting file content changes."""
    if path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")


def update_files(root: Path, new_version: str) -> None:
    """Update version strings in all required files."""
    # 1. aura/version.py
    version_file = root / "aura" / "version.py"
    v_content = version_file.read_text(encoding="utf-8")
    write_text_if_changed(
        version_file,
        re.sub(r'__version__ = "[^"]+"', f'__version__ = "{new_version}"', v_content),
    )

    # 2. pyproject.toml
    toml_file = root / "pyproject.toml"
    t_content = toml_file.read_text(encoding="utf-8")
    write_text_if_changed(
        toml_file,
        re.sub(r'^version = "[^"]+"', f'version = "{new_version}"', t_content, flags=re.MULTILINE),
    )

    # 3. README.md
    readme_file = root / "README.md"
    r_content = readme_file.read_text(encoding="utf-8")
    write_text_if_changed(
        readme_file,
        re.sub(r"badge/version-([\d.]+)-orange", f"badge/version-{new_version}-orange", r_content),
    )

    print(f"Version updated to {new_version} in version.py, pyproject.toml, and README.md")


# Helpers


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Run a subprocess command and fail loudly if it errors."""
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True, env=env)


def clean_pip_env() -> dict[str, str]:
    """Return a copy of the current environment with user pip config stripped."""
    env = os.environ.copy()
    for key in ("PIP_CONFIG_FILE", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_TRUSTED_HOST"):
        env.pop(key, None)
    env["PIP_CONFIG_FILE"] = "NUL" if sys.platform == "win32" else "/dev/null"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def create_build_venv(root: Path, *, fast: bool = False, refresh_build_venv: bool = False) -> Path:
    """Create a pristine virtual environment for the build."""
    venv_dir = root / OUTPUT_DIR / ".build_venv"

    if fast and not refresh_build_venv:
        python_exe = venv_dir / "Scripts" / "python.exe"
        if python_exe.exists():
            print("Reusing existing build venv (fast mode).")
            return python_exe
        print("Build venv not found; creating fresh one.")
    else:
        if refresh_build_venv:
            print("Refreshing build venv...")
        else:
            print("Cleaning up old build environment...")
        if venv_dir.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)

    print("Creating pristine build environment...")
    import venv

    venv.create(venv_dir, with_pip=True)

    python_exe = venv_dir / "Scripts" / "python.exe"
    if not python_exe.exists():
        raise SystemExit(f"Failed to find python executable in {venv_dir}")

    print("Installing Aura and build dependencies into pristine isolated environment...")
    run([str(python_exe), "-m", "pip", "--isolated", "install", "-e", ".", "nuitka", "zstandard"], env=clean_pip_env())

    return python_exe


def validate_project_paths(root: Path) -> None:
    """Validate required project paths before starting."""
    required_paths = [
        root / PACKAGE_NAME,
        root / PACKAGE_NAME / "__main__.py",
        root / UPDATER_HELPER_SOURCE,
        root / ICON_PATH,
        root / MEDIA_DIR,
    ]
    missing = [path for path in required_paths if not path.exists()]
    media_dir = root / MEDIA_DIR
    missing.extend(media_dir / filename for filename in REQUIRED_MEDIA_FILES if not (media_dir / filename).is_file())
    if missing:
        details = "\n".join(f"  - {path}" for path in missing)
        raise SystemExit(f"Missing required build files:\n{details}")


def clean_previous_dist_dirs(root: Path) -> None:
    """Remove stale Nuitka dist folders."""
    build_dir = root / OUTPUT_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    for dist_dir in build_dir.glob("*.dist"):
        shutil.rmtree(dist_dir, ignore_errors=True)


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
    ignore_func = shutil.ignore_patterns(
        "__pycache__", ".pytest_cache", "*.pyc", "*.pyo",
        "*.tmp", "*.swp", "*~", "*.bak",
    )

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

        target = dest / entry.name
        shutil.copytree(entry, target, ignore=ignore_func)
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

    ignore_func = shutil.ignore_patterns(
        "__pycache__", ".pytest_cache", "*.pyc", "*.pyo",
        "*.tmp", "*.swp", "*~", "*.bak",
    )

    bundled = []
    for entry in sorted(source.iterdir()):
        if not entry.is_dir():
            continue
        drone_json = entry / "drone.json"
        if not drone_json.exists():
            continue
        target = dest / entry.name
        shutil.copytree(entry, target, ignore=ignore_func)
        bundled.append(entry.name)

    if bundled:
        print(f"Bundled {len(bundled)} built-in drone(s): {', '.join(bundled)}")
    else:
        print("No built-in drones found to bundle.")


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
        print("Warning: tree-sitter grammar prewarm failed; continuing release build.")
        return

    # Verify the grammar directory is non-empty
    entries = list(grammar_dir.rglob("*"))
    if not entries:
        raise SystemExit("Grammar prewarm produced no files; aborting build.")

    print(f"Grammar prewarm complete: {len(entries)} file(s) in {grammar_dir}")


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
        raise SystemExit("Chromium bundle failed; aborting build.")

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
        raise SystemExit("Playwright bundle validation failed: imports broken.")

    print("Playwright bundle validation: OK")


def zip_distribution(root: Path, final_dist_dir: Path) -> Path:
    """Package Aura.dist into a ZIP."""
    zip_base = root / OUTPUT_DIR / "Aura-Windows-x64"
    print(f"Creating release archive: {zip_base}.zip")
    shutil.make_archive(str(zip_base), "zip", root_dir=str(final_dist_dir), base_dir=".")
    return Path(f"{zip_base}.zip")


def copy_to_desktop(zip_path: Path) -> None:
    """Copy the final ZIP to the user's desktop, handling OneDrive redirects."""
    home = Path.home()

    # Try common desktop locations, prioritizing OneDrive
    candidates = [
        home / "OneDrive" / "Desktop",
        home / "Desktop",
    ]

    target_desktop = None
    for cand in candidates:
        if cand.exists():
            target_desktop = cand
            break

    if target_desktop:
        target = target_desktop / ZIP_NAME
        shutil.copy2(zip_path, target)
        print(f"Success! Release ZIP copied to: {target}")
    else:
        print("Could not find Desktop folder to copy the release ZIP.")


def find_iscc() -> Path | None:
    """Find Inno Setup's iscc.exe compiler."""
    exe = shutil.which("iscc")
    if exe:
        return Path(exe)
    candidates = [
        "C:\\Program Files (x86)\\Inno Setup 6\\iscc.exe",
        "C:\\Program Files\\Inno Setup 6\\iscc.exe",
        "C:\\Program Files (x86)\\Inno Setup\\iscc.exe",
        "C:\\Program Files\\Inno Setup\\iscc.exe",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def create_installer(dist_dir: Path, version: str, installer_flag: bool | None) -> Path | None:
    """Build an Inno Setup installer from the dist directory."""
    if installer_flag is False:
        print("Skipping installer creation (--no-installer).")
        return None

    iscc = find_iscc()
    if installer_flag is True and iscc is None:
        raise SystemExit(
            "Cannot create installer: iscc.exe not found. Install Inno Setup 6 or ensure iscc.exe is on PATH."
        )

    if iscc is None:
        print("iscc.exe not found. Skipping installer creation.")
        print("Install Inno Setup 6 to enable installer builds.")
        return None

    root = Path(__file__).resolve().parent.parent
    iss_path = root / INSTALLER_ISS_PATH
    if not iss_path.exists():
        raise SystemExit(f"Installer script not found: {iss_path}")

    output_dir = root / OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(iscc),
        f"/DMyAppVersion={version}",
        f"/DSourceDir={dist_dir}",
        str(iss_path),
    ]
    run(cmd)

    installer_path = output_dir / f"{INSTALLER_BASE_NAME}-{version}.exe"
    if not installer_path.exists():
        raise SystemExit(
            f"Expected installer not found: {installer_path}\n"
            "Check that the ISS script outputs to the expected location."
        )

    print(f"Installer created: {installer_path}")
    return installer_path


def upload_to_github_release(installer_path: Path, version: str, create_release: bool = False) -> None:
    """Upload the installer to the GitHub release for tag v{version} using gh CLI."""
    tag = f"v{version}"

    # Check gh CLI is available
    if not shutil.which("gh"):
        raise SystemExit(
            "GitHub CLI (gh) not found.\n"
            "Install it from https://cli.github.com/ and ensure it's on your PATH."
        )

    # Check gh auth status
    auth_result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True,
    )
    if auth_result.returncode != 0:
        raise SystemExit(
            "GitHub CLI is not authenticated. Run 'gh auth login' first.\n"
            f"Error: {auth_result.stderr.strip()}"
        )

    view_result = subprocess.run(
        ["gh", "release", "view", tag, "--json", "tagName"],
        capture_output=True, text=True,
    )
    release_exists = view_result.returncode == 0

    if not release_exists:
        if not create_release:
            raise SystemExit(
                f"GitHub release {tag} does not exist. "
                "Use --create-github-release to create it."
            )
        print(f"Creating GitHub release {tag}...")
        run(["gh", "release", "create", tag, f"--title=Aura v{version}", "--generate-notes"])

    # Upload installer asset
    print(f"Uploading {installer_path.name} to GitHub release {tag}...")
    run(["gh", "release", "upload", tag, str(installer_path), "--clobber"])
    print("Upload complete.")


def create_nuitka_command(
    python_exe: Path | None = None,
    *,
    low_memory: bool = True,
    jobs: int = DEFAULT_NUITKA_JOBS,
    fast: bool = False,
) -> list[str]:
    """Create the Nuitka command used for release builds."""
    if jobs == 0:
        raise SystemExit("--jobs cannot be 0.")
    python_exe = python_exe or Path(sys.executable)

    cmd = [
        str(python_exe),
        "-m",
        "nuitka",
        "--standalone",
        "--enable-plugin=pyside6",
        "--windows-console-mode=disable",
        f"--windows-icon-from-ico={ICON_PATH}",
        f"--include-data-dir={MEDIA_DIR}={MEDIA_DIR}",
        "--include-package=aura",
        "--include-package-data=aura",
        "--include-package=playwright",
        "--include-package-data=playwright",
        f"--include-data-file={UPDATER_HELPER_SOURCE}={UPDATER_HELPER_DIST_NAME}",
        f"--output-dir={OUTPUT_DIR}",
        f"--output-filename={APP_NAME}",
        "--assume-yes-for-downloads",
        "--python-flag=-m",
        "--nofollow-import-to=google",
        "--nofollow-import-to=libcst",
        "--nofollow-import-to=numpy",
        "--nofollow-import-to=scipy",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=charset_normalizer",
        "--nofollow-import-to=click",
        "--lto=no",
    ]
    if not fast:
        cmd.insert(cmd.index("--assume-yes-for-downloads"), "--clean-cache=all")
    if low_memory:
        cmd.append("--low-memory")
    cmd.append(f"--jobs={jobs}")

    if SIGN_CERT:
        cmd.append(f"--windows-sign-certificate={SIGN_CERT}")
        if SIGN_PASS:
            cmd.append(f"--windows-sign-certificate-password={SIGN_PASS}")

    cmd.append(PACKAGE_NAME)
    return cmd


# Main Build Flow


def resolve_build_version(
    root: Path,
    requested_version: str | None,
    *,
    skip_version_update: bool = False,
) -> str:
    """Resolve the build version, updating files only when explicitly requested."""
    if requested_version is not None and skip_version_update:
        raise SystemExit("--version and --skip-version-update cannot be used together.")

    if requested_version is not None:
        version = normalize_version(requested_version)
        update_files(root, version)
        return version

    if skip_version_update:
        version = read_current_version(root)
        print(f"Using current version: {version}")
        return version

    # Default to interactive if not skipping and no version provided
    current = read_current_version(root)
    raw = input(f"Enter release version X.Y.Z or leave blank to keep {current}: ").strip()
    if raw:
        version = normalize_version(raw)
        update_files(root, version)
        return version

    print(f"Using current version: {current}")
    return current



def build(
    version: str | None = None,
    *,
    skip_version_update: bool = False,
    copy_desktop: bool = True,
    low_memory: bool = True,
    jobs: int = DEFAULT_NUITKA_JOBS,
    installer: bool | None = None,
    fast: bool = False,
    refresh_build_venv: bool = False,
    installer_only: bool = False,
    github_release: bool = False,
    create_github_release: bool = False,
) -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # 1. Versioning
    new_version = resolve_build_version(
        root,
        version,
        skip_version_update=skip_version_update,
    )

    # 2. Validation & Cleanup
    validate_project_paths(root)
    clean_previous_dist_dirs(root)

    # 3. Build Environment
    python_exe = create_build_venv(root, fast=fast, refresh_build_venv=refresh_build_venv)

    # 4. Nuitka Command
    cmd = create_nuitka_command(python_exe, low_memory=low_memory, jobs=jobs, fast=fast)

    # 5. Run Build
    print(f"\nStarting Nuitka build for version {new_version}...")
    try:
        run(cmd)
    except subprocess.CalledProcessError:
        print("\nBuild failed.")
        sys.exit(1)

    # 6. Package & Deploy
    dist_dir = find_created_dist_dir(root)
    final_dist_dir = normalize_dist_dir(root, dist_dir)

    # Helper to find a package path inside the clean venv
    def get_venv_package_path(pkg: str) -> Path | None:
        try:
            out = subprocess.check_output(
                [str(python_exe), "-c", f"import {pkg}; print({pkg}.__file__)"], text=True, stderr=subprocess.DEVNULL
            ).strip()
            return Path(out).resolve().parent
        except subprocess.CalledProcessError:
            return None

    # Copy google-genai as pure Python files to avoid Nuitka compilation hang/crash
    google_genai_path = get_venv_package_path("google.genai")
    if google_genai_path and google_genai_path.exists():
        target_google_dir: Path = final_dist_dir / "google"
        target_genai_dir: Path = target_google_dir / "genai"
        print(f"Bundling google-genai as raw source: {google_genai_path} -> {target_genai_dir}")
        if target_genai_dir.exists():
            shutil.rmtree(target_genai_dir)
        shutil.copytree(google_genai_path, target_genai_dir)
    else:
        print("Warning: google-genai is not installed in the clean environment, skipping manual bundle.")

    # Copy libcst as pure Python files to avoid Nuitka compilation hang/crash
    libcst_path = get_venv_package_path("libcst")
    if libcst_path and libcst_path.exists():
        target_libcst_dir: Path = final_dist_dir / "libcst"
        print(f"Bundling libcst as raw source: {libcst_path} -> {target_libcst_dir}")
        if target_libcst_dir.exists():
            shutil.rmtree(target_libcst_dir)
        shutil.copytree(libcst_path, target_libcst_dir)
    else:
        print("Warning: libcst is not installed in the clean environment, skipping manual bundle.")

    # Copy charset_normalizer as pure Python files to avoid Nuitka compilation hang/crash
    charset_normalizer_path = get_venv_package_path("charset_normalizer")
    if charset_normalizer_path and charset_normalizer_path.exists():
        target_charset_dir: Path = final_dist_dir / "charset_normalizer"
        print(f"Bundling charset_normalizer as raw source: {charset_normalizer_path} -> {target_charset_dir}")
        if target_charset_dir.exists():
            shutil.rmtree(target_charset_dir)
        shutil.copytree(charset_normalizer_path, target_charset_dir)
    else:
        print("Warning: charset_normalizer is not installed in the clean environment, skipping manual bundle.")

    # Copy click as pure Python files to avoid Nuitka C compiler crash
    click_path = get_venv_package_path("click")
    if click_path and click_path.exists():
        target_click_dir: Path = final_dist_dir / "click"
        print(f"Bundling click as raw source: {click_path} -> {target_click_dir}")
        if target_click_dir.exists():
            shutil.rmtree(target_click_dir)
        shutil.copytree(click_path, target_click_dir)
    else:
        print("Warning: click is not installed in the clean environment, skipping manual bundle.")



    # Bundle .aura/drones into the dist for DroneStore runtime loading
    bundle_drones(root, final_dist_dir)

    # Bundle built-in drone templates (e.g. Browse Monitor) into the dist
    bundle_builtin_drones(root, final_dist_dir)

    # Pre-warm tree-sitter grammars into the dist
    prewarm_grammars(final_dist_dir, python_exe)

    # Bundle Chromium browser for Playwright
    bundle_chromium(final_dist_dir, python_exe)

    # Validate Playwright bundle
    validate_playwright_bundle(final_dist_dir, python_exe)

    if not installer_only:
        zip_path = zip_distribution(root, final_dist_dir)
        if copy_desktop:
            copy_to_desktop(zip_path)

    installer_path = create_installer(final_dist_dir, new_version, installer)
    if installer_path:
        print(f"Installer created at: {installer_path}")

    if github_release:
        if installer_path:
            upload_to_github_release(installer_path, new_version, create_github_release)
        else:
            raise SystemExit(
                "--github-release requires an installer. Use --installer or --installer-only "
                "and ensure Inno Setup is available."
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Build Aura with Nuitka.")
    parser.add_argument(
        "--version",
        help=("Set project version before building. When omitted, the user is prompted to enter a version (default)."),
    )
    parser.add_argument(
        "--skip-version-update",
        action="store_true",
        help="Use the current project version without prompting or editing files.",
    )
    parser.add_argument(
        "--no-copy-desktop",
        action="store_true",
        help="Do not copy the release ZIP to the Desktop after packaging.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_NUITKA_JOBS,
        help=(
            "Number of parallel C compiler jobs for Nuitka. Defaults to 1 to "
            "avoid MSVC heap exhaustion on large generated modules."
        ),
    )
    parser.add_argument(
        "--no-low-memory",
        action="store_true",
        help="Disable Nuitka low-memory mode. This may be faster but can trigger MSVC heap exhaustion.",
    )
    parser.add_argument(
        "--installer",
        action="store_true",
        default=None,
        help="Enable installer creation. Auto-detects if iscc.exe is available.",
    )
    parser.add_argument(
        "--no-installer",
        action="store_true",
        help="Explicitly skip installer creation.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Fast build: reuse build venv, skip Nuitka cache cleanup. Useful for quick hotfix builds.",
    )
    parser.add_argument(
        "--refresh-build-venv",
        action="store_true",
        help="Force re-creation of the build venv even in fast mode.",
    )
    parser.add_argument(
        "--installer-only",
        action="store_true",
        help="Skip ZIP and desktop copy; build the installer only. Implies --installer.",
    )
    parser.add_argument(
        "--github-release",
        action="store_true",
        help="Upload the installer to GitHub Releases using gh CLI after a successful build.",
    )
    parser.add_argument(
        "--create-github-release",
        action="store_true",
        help="Create the GitHub release if it does not exist. Only meaningful with --github-release.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])

    if args.create_github_release and not args.github_release:
        print("Warning: --create-github-release has no effect without --github-release.")

    installer: bool | None = None
    if args.installer_only:
        installer = True
    elif args.installer:
        installer = True
    if args.no_installer and not args.installer_only:
        installer = False

    build(
        args.version,
        skip_version_update=args.skip_version_update,
        copy_desktop=not args.no_copy_desktop,
        low_memory=not args.no_low_memory,
        jobs=args.jobs,
        installer=installer,
        fast=args.fast,
        refresh_build_venv=args.refresh_build_venv,
        installer_only=args.installer_only,
        github_release=args.github_release,
        create_github_release=args.create_github_release,
    )
