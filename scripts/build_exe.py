"""Build script for Aura EXE using PyInstaller."""
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
    print(f"Starting build for {APP_NAME}...")
    
    # Ensure we are in the project root
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # Command construction
    # --onefile: single EXE
    # --noconsole: hide console window
    # --icon: set app icon
    # --add-data: include media files
    # --hidden-import: ensure dynamic imports are caught
    
    # On Windows, add-data uses ';' separator
    sep = ";" if sys.platform == "win32" else ":"
    
    cmd = [
        "pyinstaller",
        "--onefile",
        "--noconsole",
        f"--icon={ICON_PATH}",
        f"--add-data={MEDIA_DIR}{sep}{MEDIA_DIR}",
        "--name=Aura",
        "--clean",
        # Ensure PySide6 and other dynamic dependencies are included
        "--hidden-import=PySide6.QtWebEngineWidgets",
        "--hidden-import=PySide6.QtWebChannel",
        ENTRY_POINT
    ]

    print(f"Running: {' '.join(cmd)}")
    
    try:
        subprocess.run(cmd, check=True)
        print("\nBuild successful! The EXE is in the 'dist' folder.")
    except subprocess.CalledProcessError as e:
        print(f"\nBuild failed with exit code {e.returncode}")
        sys.exit(e.returncode)

if __name__ == "__main__":
    # Check if pyinstaller is installed
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Installing it now...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)
    
    build()
