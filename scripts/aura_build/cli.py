"""Nuitka command construction, phase timing, build orchestration, and CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from scripts.aura_build.assets import (
    bundle_builtin_drones,
    bundle_chromium,
    bundle_drones,
    copy_raw_source_packages,
    directory_size,
    find_created_dist_dir,
    format_size,
    normalize_dist_dir,
    prewarm_grammars,
    validate_playwright_bundle,
)
from scripts.aura_build.config import (
    APP_NAME,
    BUNDLED_SKILLS_DEST_REL,
    BUNDLED_SKILLS_SOURCE_REL,
    DEFAULT_NUITKA_JOBS,
    ICON_PATH,
    MAX_DEFAULT_NUITKA_JOBS,
    MEDIA_DIR,
    NUITKA_REPORT_PATH,
    OUTPUT_DIR,
    PACKAGE_NAME,
    PRODUCTION_PROMPT_DEST_REL,
    PRODUCTION_PROMPT_SOURCE_REL,
    SIGN_CERT_ENV,
    SIGN_PASS_ENV,
    UPDATER_HELPER_DIST_NAME,
    UPDATER_HELPER_SOURCE,
)
from scripts.aura_build.environment import (
    clean_previous_dist_dirs,
    prepare_build_venv,
    resolve_build_version,
    run,
    validate_project_paths,
)
from scripts.aura_build.release import (
    create_installer,
    upload_to_github_release,
    validate_release_prerequisites,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# Job resolution


def resolve_effective_jobs(jobs: int | None, *, low_memory: bool) -> int:
    """Resolve the compiler job count actually handed to Nuitka.

    Raises before any compilation is launched when the requested count is
    invalid. Low-memory mode always collapses to a single job, because Nuitka's
    --low-memory serializes compilation regardless of --jobs.
    """
    requested = DEFAULT_NUITKA_JOBS if jobs is None else jobs
    if requested <= 0:
        raise SystemExit("--jobs must be greater than 0.")
    return 1 if low_memory else requested


# Phase timing


def format_duration(seconds: float) -> str:
    """Render an elapsed duration as ``MmSSs``."""
    total = int(round(seconds))
    return f"{total // 60}m {total % 60:02d}s"


@dataclass
class PhaseRecord:
    """One timed build phase."""

    name: str
    seconds: float = 0.0
    failed: bool = False


@dataclass
class BuildTimeline:
    """Monotonic phase timing plus the facts the final summary must report."""

    phases: list[PhaseRecord] = field(default_factory=list)
    venv_reused: bool | None = None
    clean_build: bool = False
    low_memory: bool = False
    requested_jobs: int | None = None
    effective_jobs: int = 0
    dist_bytes: int | None = None
    installer_bytes: int | None = None
    _started: float = field(default_factory=time.monotonic)

    @contextmanager
    def phase(self, name: str) -> Iterator[PhaseRecord]:
        """Time a phase, reporting start and completion as the work happens."""
        record = PhaseRecord(name=name)
        self.phases.append(record)
        print(f"\n>>> [{len(self.phases)}] {name}: starting...", flush=True)
        started = time.monotonic()
        try:
            yield record
        except BaseException:
            record.seconds = time.monotonic() - started
            record.failed = True
            print(f"<<< {name}: FAILED after {format_duration(record.seconds)}", flush=True)
            raise
        record.seconds = time.monotonic() - started
        print(f"<<< {name}: done in {format_duration(record.seconds)}", flush=True)

    @property
    def total_seconds(self) -> float:
        return time.monotonic() - self._started

    def _venv_label(self) -> str:
        if self.venv_reused is None:
            return "not prepared"
        return "reused" if self.venv_reused else "refreshed"

    def _jobs_label(self) -> str:
        if self.low_memory:
            return f"{self.effective_jobs} (low-memory mode forces a single job)"
        return str(self.effective_jobs)

    def summary_lines(self) -> list[str]:
        """Build the final summary, valid whether or not every phase ran."""
        width = max((len(record.name) for record in self.phases), default=0)
        lines = [
            "",
            "=" * 62,
            f"Build summary - total elapsed {format_duration(self.total_seconds)}",
            "=" * 62,
        ]
        if self.phases:
            lines.append("Phases (execution order):")
            for index, record in enumerate(self.phases, start=1):
                status = "  FAILED" if record.failed else ""
                lines.append(
                    f"  {index}. {record.name.ljust(width)}  {format_duration(record.seconds)}{status}"
                )
        else:
            lines.append("Phases: none completed.")
        lines.extend([
            f"Build mode:        {'clean (Nuitka caches cleared)' if self.clean_build else 'default (Nuitka caches preserved)'}",
            f"Build venv:        {self._venv_label()}",
            f"Low memory:        {'on' if self.low_memory else 'off'}",
            f"Effective jobs:    {self._jobs_label()}",
            f"Distribution size: {format_size(self.dist_bytes)}",
        ])
        if self.installer_bytes is not None:
            lines.append(f"Installer size:    {format_size(self.installer_bytes)}")
        else:
            lines.append("Installer:         not created")
        lines.append("=" * 62)
        return lines

    def print_summary(self) -> None:
        for line in self.summary_lines():
            print(line, flush=True)


# Nuitka command


def signing_arguments() -> list[str]:
    """Return Nuitka signing flags from the environment, when configured."""
    cert = os.environ.get(SIGN_CERT_ENV)
    if not cert:
        return []
    args = [f"--windows-sign-certificate={cert}"]
    password = os.environ.get(SIGN_PASS_ENV)
    if password:
        args.append(f"--windows-sign-certificate-password={password}")
    return args


def create_nuitka_command(
    python_exe: Path | None = None,
    *,
    low_memory: bool = False,
    jobs: int = DEFAULT_NUITKA_JOBS,
    clean: bool = False,
) -> list[str]:
    """Create the Nuitka command used for release builds."""
    if jobs <= 0:
        raise SystemExit("--jobs must be greater than 0.")
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
        f"--include-data-file={PRODUCTION_PROMPT_SOURCE_REL}={PRODUCTION_PROMPT_DEST_REL}",
        f"--include-data-dir={BUNDLED_SKILLS_SOURCE_REL}={BUNDLED_SKILLS_DEST_REL}",
        "--include-package=aura",
        "--include-package-data=aura",
        "--include-package=relay",
        "--include-package=fastapi",
        "--include-package=uvicorn",
        "--include-package=playwright",
        "--include-package-data=playwright",
        f"--include-data-file={UPDATER_HELPER_SOURCE}={UPDATER_HELPER_DIST_NAME}",
        f"--output-dir={OUTPUT_DIR}",
        f"--output-filename={APP_NAME}",
        "--assume-yes-for-downloads",
        "--python-flag=-m",
        "--nofollow-import-to=google",
        "--nofollow-import-to=scipy",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=charset_normalizer",
        "--nofollow-import-to=click",
        "--lto=no",
        f"--report={NUITKA_REPORT_PATH}",
        "--report-diffable",
    ]
    if clean:
        cmd.insert(cmd.index("--assume-yes-for-downloads"), "--clean-cache=all")
    if low_memory:
        cmd.append("--low-memory")
    cmd.append(f"--jobs={jobs}")

    cmd.extend(signing_arguments())

    cmd.append(PACKAGE_NAME)
    return cmd


# Build orchestration


def build(
    version: str | None = None,
    *,
    skip_version_update: bool = False,
    low_memory: bool = False,
    jobs: int | None = None,
    installer: bool | None = None,
    clean: bool = False,
    github_release: bool = False,
    create_github_release: bool = False,
) -> None:
    """Run the full Windows build, always reporting the phases that ran."""
    # Resolved first so an invalid job count fails before anything is launched.
    effective_jobs = resolve_effective_jobs(jobs, low_memory=low_memory)

    root = REPO_ROOT
    os.chdir(root)

    timeline = BuildTimeline(
        clean_build=clean,
        low_memory=low_memory,
        requested_jobs=jobs,
        effective_jobs=effective_jobs,
    )

    print(f"Build mode: {'clean' if clean else 'default'} "
          f"({'Nuitka caches cleared, build venv recreated' if clean else 'Nuitka caches and build venv reused when compatible'})")
    if low_memory:
        requested = DEFAULT_NUITKA_JOBS if jobs is None else jobs
        print(f"Low-memory mode: ON - forcing a single compilation job (requested {requested}).")
    print(f"Effective Nuitka jobs: {effective_jobs}")

    try:
        _run_phases(
            root,
            timeline,
            version=version,
            skip_version_update=skip_version_update,
            low_memory=low_memory,
            effective_jobs=effective_jobs,
            installer=installer,
            clean=clean,
            github_release=github_release,
            create_github_release=create_github_release,
        )
    finally:
        # Never swallow the original failure: report, then let it propagate.
        timeline.print_summary()


def _run_phases(
    root: Path,
    timeline: BuildTimeline,
    *,
    version: str | None,
    skip_version_update: bool,
    low_memory: bool,
    effective_jobs: int,
    installer: bool | None,
    clean: bool,
    github_release: bool,
    create_github_release: bool,
) -> None:
    with timeline.phase("prerequisites & version resolution"):
        validate_project_paths(root)
        validate_release_prerequisites(installer, github_release)
        new_version = resolve_build_version(root, version, skip_version_update=skip_version_update)
        clean_previous_dist_dirs(root)

    with timeline.phase("build environment preparation"):
        build_venv = prepare_build_venv(root, clean=clean)
        timeline.venv_reused = build_venv.reused

    python_exe = build_venv.python_exe
    cmd = create_nuitka_command(python_exe, low_memory=low_memory, jobs=effective_jobs, clean=clean)

    with timeline.phase("Nuitka compilation"):
        print(f"Starting Nuitka build for version {new_version}...")
        try:
            run(cmd)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"Nuitka compilation failed (exit code {exc.returncode}).") from exc
        final_dist_dir = normalize_dist_dir(root, find_created_dist_dir(root))

    with timeline.phase("manual package copies"):
        copy_raw_source_packages(final_dist_dir, python_exe)

    with timeline.phase("drone & resource preparation"):
        bundle_drones(root, final_dist_dir)
        bundle_builtin_drones(root, final_dist_dir)

    with timeline.phase("grammar preparation"):
        prewarm_grammars(final_dist_dir, python_exe)

    with timeline.phase("Chromium preparation & validation"):
        bundle_chromium(final_dist_dir, python_exe)
        validate_playwright_bundle(final_dist_dir, python_exe)

    timeline.dist_bytes = directory_size(final_dist_dir)

    installer_path: Path | None = None
    with timeline.phase("installer creation"):
        installer_path = create_installer(root, final_dist_dir, new_version, installer)
        if installer_path is not None:
            timeline.installer_bytes = installer_path.stat().st_size
            print(f"Installer created at: {installer_path}")

    if github_release:
        with timeline.phase("GitHub upload"):
            if installer_path is None:
                raise SystemExit(
                    "--github-release requires an installer. Use --installer and ensure Inno Setup is available."
                )
            upload_to_github_release(installer_path, new_version, create_github_release)


# CLI


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
        "--jobs",
        type=int,
        default=None,
        help=(
            "Number of parallel C compiler jobs for Nuitka. Defaults to "
            f"min({MAX_DEFAULT_NUITKA_JOBS}, cpu_count // 2) = {DEFAULT_NUITKA_JOBS} on this machine. "
            "Ignored under --low-memory, which always compiles with a single job."
        ),
    )
    parser.add_argument(
        "--low-memory",
        action="store_true",
        help=(
            "Fallback for memory-constrained machines: enable Nuitka's --low-memory mode, "
            "which forces the effective job count to 1."
        ),
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Intentional cold build: recreate the build venv and pass --clean-cache=all to Nuitka.",
    )
    installer_group = parser.add_mutually_exclusive_group()
    installer_group.add_argument(
        "--installer",
        action="store_true",
        default=None,
        help="Enable installer creation. Auto-detects if iscc.exe is available.",
    )
    installer_group.add_argument(
        "--no-installer",
        action="store_true",
        help="Explicitly skip installer creation.",
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


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``python scripts/build_nuitka.py``."""
    # Keep phase messages interleaved correctly with subprocess output when the
    # build log is redirected to a file.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.create_github_release and not args.github_release:
        print("Warning: --create-github-release has no effect without --github-release.")

    installer: bool | None = None
    if args.installer:
        installer = True
    if args.no_installer:
        installer = False

    build(
        args.version,
        skip_version_update=args.skip_version_update,
        low_memory=args.low_memory,
        jobs=args.jobs,
        installer=installer,
        clean=args.clean,
        github_release=args.github_release,
        create_github_release=args.create_github_release,
    )
