"""Build script for Aura EXE using Nuitka (compiled for professional distribution)."""
import os
import subprocess
import sys
from pathlib import Path

# Configuration
APP_NAME = "Aura"
ENTRY_POINT = "aura/__main__.py"
ICON_PATH = "media/AurA.ico"
MEDIA_DIR = "media"

def build():
    print(f"Starting Nuitka build for {APP_NAME}...")
    
    # Ensure we are in the project root
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # Nuitka command construction
    # --standalone: produce a folder with all dependencies
    # --onefile: compress into a single EXE
    # --enable-plugin=pyside6: critical for Qt apps
    # --windows-console-mode=disable: hide console
    # --windows-icon-from-ico: set app icon
    # --include-data-dir: bundle media/ folder
    # --follow-imports: compile all dependencies
    
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--onefile",
        "--enable-plugin=pyside6",
        "--windows-console-mode=disable",
        f"--windows-icon-from-ico={ICON_PATH}",
        f"--include-data-dir={MEDIA_DIR}={MEDIA_DIR}",
        "--output-dir=build",
        f"--output-filename={APP_NAME}",
        "--clean-cache",
        "--assume-yes-for-downloads",
        ENTRY_POINT
    ]

    print(f"Running: {' '.join(cmd)}")
    
    try:
        # Note: Nuitka builds take a significant amount of time as they compile to C++.
        subprocess.run(cmd, check=True)
        print(f"\nBuild successful! The compiled EXE is in the 'build' folder.")
    except subprocess.CalledProcessError as e:
        print(f"\nBuild failed with exit code {e.returncode}")
        sys.exit(e.returncode)

if __name__ == "__main__":
    # Check if nuitka and zstandard (for compression) are installed
    try:
        import nuitka
        import zstandard
    except ImportError:
        print("Required build dependencies (nuitka, zstandard) not found. Installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "nuitka", "zstandard"], check=True)
    
    build()
