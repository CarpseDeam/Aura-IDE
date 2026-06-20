from __future__ import annotations

import re
import subprocess
from pathlib import Path

from aura.project_env import preferred_python_for_compile


def path_to_module(path: str) -> str | None:
    """Convert a workspace-relative .py path to its dotted module name.

    Returns None for paths outside aura/ and for __main__.py.
    """
    if not path.startswith("aura/"):
        return None
    if not path.endswith(".py"):
        return None

    stripped = path.removesuffix(".py")

    if stripped.endswith("/__main__"):
        return None

    if stripped.endswith("/__init__"):
        stripped = stripped.removesuffix("/__init__")

    return stripped.replace("/", ".")


def run_focused_import_check(
    workspace_root: Path, paths: list[str]
) -> tuple[bool, str]:
    if not paths:
        return True, ""

    python_exe = preferred_python_for_compile(workspace_root)
    found_real_failure = False
    outputs: list[str] = []

    for path in sorted(paths):
        module = path_to_module(path)
        if module is None:
            outputs.append(f"{path}: skipped")
            continue

        try:
            result = subprocess.run(
                [str(python_exe), "-c", f"import {module}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(workspace_root),
            )
        except subprocess.TimeoutExpired:
            outputs.append(f"{path} \u2192 {module}: (check skipped \u2014 timed out)")
            continue
        except (FileNotFoundError, OSError) as exc:
            outputs.append(
                f"{path} \u2192 {module}: (check skipped \u2014 {exc})"
            )
            continue

        output = result.stdout + result.stderr
        if result.returncode == 0:
            outputs.append(f"{path} \u2192 {module}: imported ok")
        elif _is_import_error(output):
            found_real_failure = True
            outputs.append(f"{path} \u2192 {module}: IMPORT FAILED\n{output}")
        elif _is_shell_failure(output):
            outputs.append(
                f"{path} \u2192 {module}: infrastructure error (check could not run)\n{output}"
            )
        else:
            found_real_failure = True
            outputs.append(f"{path} \u2192 {module}: FAILED\n{output}")

    return not found_real_failure, "\n".join(outputs)


def _is_import_error(output: str) -> bool:
    lower = output.lower()
    # Check for explicit Python import error markers
    if "importerror" in lower or "modulenotfounderror" in lower:
        return True
    # Check for traceback + file/line pattern
    if "traceback (most recent call last)" in lower:
        return True
    if re.search(r'file ".*?", line \d+', output):
        return True
    return False


def _is_shell_failure(output: str) -> bool:
    lower = output.lower()
    markers = [
        "cannot find the path specified",
        "not recognized",
        "no such file or directory",
        "command not found",
        "not found",
    ]
    return any(marker in lower for marker in markers)
