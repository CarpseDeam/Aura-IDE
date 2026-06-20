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


def _import_one_module(
    python_exe: Path,
    module: str,
    cwd: Path,
    timeout: int = 30,
) -> tuple[bool, str, bool]:
    """Run `python_exe -c \"import {module}\"` and return (ok, output, is_infra).

    ok=True when returncode==0.
    is_infra=True when the failure is NOT a real Python error:
      timeout, FileNotFoundError, OSError, or _is_shell_failure(output) is True
      (only when no traceback is present).
    is_infra=False when failure is a real Python error (ImportError, traceback,
      or any non-zero output that isn't a shell failure).

    Real Python errors (_is_import_error) are checked first so that a module
    raising an ImportError/FileNotFoundError at import time is never mistaken
    for an infrastructure failure, even if the output also happens to contain
    a shell-failure marker string.
    """
    try:
        result = subprocess.run(
            [str(python_exe), "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        return False, "(timed out)", True
    except (FileNotFoundError, OSError) as exc:
        return False, f"({exc})", True

    output = result.stdout + result.stderr
    if result.returncode == 0:
        return True, output, False

    # Real Python errors (traceback, ImportError, ModuleNotFoundError) are
    # detected first so they are never misclassified as infrastructure failures.
    if _is_import_error(output):
        return False, output, False

    if _is_shell_failure(output):
        return False, output, True

    return False, output, False


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

        ok, output, is_infra = _import_one_module(
            python_exe, module, workspace_root
        )
        if ok:
            outputs.append(f"{path} \u2192 {module}: imported ok")
        elif is_infra:
            outputs.append(
                f"{path} \u2192 {module}: infrastructure error (check could not run)\n{output}"
            )
        elif _is_import_error(output):
            found_real_failure = True
            outputs.append(f"{path} \u2192 {module}: IMPORT FAILED\n{output}")
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


def run_dependent_import_check(
    workspace_root: Path,
    edited_paths: list[str],
    dependent_paths: list[str],
) -> tuple[list[str], str, str]:
    """Check downstream dependents for import failures caused by edited files.

    Returns (gating_paths, gating_diagnostics, informational_diagnostics).
    A failure is gating iff an edited module name appears verbatim (word-boundary
    match) in the dependent's failure output. Otherwise it is informational.
    """
    if not dependent_paths or not edited_paths:
        return [], "", ""

    python_exe = preferred_python_for_compile(workspace_root)

    edited_module_names: set[str] = set()
    for p in edited_paths:
        m = path_to_module(p)
        if m:
            edited_module_names.add(m)

    gating_paths: list[str] = []
    gating_outputs: list[str] = []
    info_outputs: list[str] = []

    for dep_path in sorted(dependent_paths):
        module = path_to_module(dep_path)
        if module is None:
            continue

        ok, output, is_infra = _import_one_module(
            python_exe, module, workspace_root
        )
        if ok or is_infra:
            continue

        is_gating = False
        for edited_module in edited_module_names:
            if re.search(
                r"(?<![a-zA-Z0-9_])" + re.escape(edited_module) + r"(?![a-zA-Z0-9_])",
                output,
            ):
                is_gating = True
                break

        if is_gating:
            gating_paths.append(dep_path)
            gating_outputs.append(
                f"{dep_path} \u2192 {module}: IMPORT FAILED (contract break)\n{output}"
            )
        else:
            info_outputs.append(
                f"{dep_path} \u2192 {module}: (unrelated)\n{output}"
            )

    gating_diagnostics = "\n".join(gating_outputs)
    informational_diagnostics = "\n".join(info_outputs)
    return gating_paths, gating_diagnostics, informational_diagnostics
