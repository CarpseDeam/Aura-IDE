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

# Signing Configuration (Set these in your environment)
# For example: $env:AURA_SIGN_CERT="C:\path\to\cert.pfx"; $env:AURA_SIGN_PASS="password"
SIGN_CERT = os.environ.get("AURA_SIGN_CERT")
SIGN_PASS = os.environ.get("AURA_SIGN_PASS")

def build():
    print(f"Starting Nuitka build for {APP_NAME}...")
    
    # Ensure we are in the project root
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # Nuitka command construction
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--enable-plugin=pyside6",
        "--windows-console-mode=disable",
        f"--windows-icon-from-ico={ICON_PATH}",
        f"--include-data-dir={MEDIA_DIR}={MEDIA_DIR}",
        "--output-dir=build",
        f"--output-filename={APP_NAME}",
        "--clean-cache=all",
        "--assume-yes-for-downloads",
        "--python-flag=-m",
        "aura"
    ]

    # Add signing flags if credentials are provided
    if SIGN_CERT:
        print(f"Adding code signing using certificate: {SIGN_CERT}")
        cmd.append(f"--windows-sign-certificate={SIGN_CERT}")
        if SIGN_PASS:
            cmd.append(f"--windows-sign-certificate-password={SIGN_PASS}")
        # Optionally specify the path to signtool.exe if not in PATH
        # cmd.append("--windows-sign-tool-path=C:/Path/To/signtool.exe")
    else:
        print("Warning: No signing certificate found in environment (AURA_SIGN_CERT). EXE will be unsigned.")

    print(f"Running: {' '.join(cmd)}")
    
    try:
        # Note: Nuitka builds take a significant amount of time as they compile to C++.
        subprocess.run(cmd, check=True)
        print(f"\nBuild successful! The compiled application is in the 'build/{APP_NAME}.dist' folder.")
        print("To distribute, zip the entire .dist directory.")
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
