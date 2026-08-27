"""Shared configuration constants for the Aura Windows build."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Aura"
PACKAGE_NAME = "aura"

ICON_PATH = "media/AurA.ico"
MEDIA_DIR = "media"
OUTPUT_DIR = "build"

# Conservative parallel default: never oversubscribe a developer workstation.
MAX_DEFAULT_NUITKA_JOBS = 4
DEFAULT_NUITKA_JOBS = min(MAX_DEFAULT_NUITKA_JOBS, max(1, (os.cpu_count() or 2) // 2))

FINAL_DIST_NAME = f"{APP_NAME}.dist"
FINAL_EXE_NAME = f"{APP_NAME}.exe"
UPDATER_HELPER_SOURCE = Path(PACKAGE_NAME) / "windows_updater.cmd"
UPDATER_HELPER_DIST_NAME = "AuraUpdater.cmd"

DRONES_SOURCE_REL = Path(".aura") / "drones"
DRONES_DEST_REL = Path(".aura") / "drones"

BUILTIN_DRONES_SOURCE_REL = Path("aura") / "drones" / "bundled"
BUILTIN_DRONES_DEST_REL = Path("aura") / "drones" / "bundled"

PRODUCTION_PROMPT_SOURCE_REL = Path("aura") / "production_prompt.md"
PRODUCTION_PROMPT_DEST_REL = Path("aura") / "production_prompt.md"

BUNDLED_SKILLS_SOURCE_REL = Path("aura") / "skills" / "bundled"
BUNDLED_SKILLS_DEST_REL = Path("aura") / "skills" / "bundled"

# Build venv reuse
BUILD_VENV_REL = Path(OUTPUT_DIR) / ".build_venv"
BUILD_VENV_MARKER_NAME = ".aura_build_venv.json"
# Owned by the build tooling: bump whenever the marker contents or the venv
# provisioning steps change in a way that makes an existing venv unusable.
BUILD_VENV_MARKER_SCHEMA = 1
# The exact dependency specification installed into the build venv.
BUILD_REQUIREMENTS = ("-e", ".", "nuitka", "zstandard")

# Nuitka compilation report (regenerated every build, lives under the ignored build/ dir).
NUITKA_REPORT_PATH = f"{OUTPUT_DIR}/nuitka-compilation-report.xml"

# Signing configuration (environment driven).
SIGN_CERT_ENV = "AURA_SIGN_CERT"
SIGN_PASS_ENV = "AURA_SIGN_PASS"

# Installer configuration
INSTALLER_ISS_PATH = "scripts/installer/Aura.iss"
INSTALLER_BASE_NAME = "AuraSetup"

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

# Packages copied into the dist as raw source because Nuitka cannot compile them
# reliably: (import name, destination path relative to the dist root).
RAW_SOURCE_PACKAGES = (
    ("google.genai", Path("google") / "genai"),
    ("charset_normalizer", Path("charset_normalizer")),
    ("click", Path("click")),
)
