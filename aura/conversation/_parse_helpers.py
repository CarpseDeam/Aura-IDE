"""Private parsing helpers for validation command processing."""
from __future__ import annotations

import re
import shlex

_OUTCOME_TOKENS = {
    "pass",
    "passed",
    "passes",
    "success",
    "succeeds",
    "succeed",
    "green",
}

_KNOWN_COMMANDS = {
    "python",
    "python3",
    "python.exe",
    "py",
    "pytest",
    "unittest",
    "ruff",
    "mypy",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "node",
    "cargo",
    "go",
    "tox",
    "make",
    "uv",
    "poetry",
    "pipenv",
    "dotnet",
    "mvn",
    "gradle",
    "rg",
    "grep",
    "findstr",
    "find",
    "cd",
    "chdir",
}


def _select_command_line(text: str) -> str:
    fenced = re.search(r"```(?:[A-Za-z0-9_-]+)?\s*\n(.*?)```", text, flags=re.DOTALL)
    source = fenced.group(1) if fenced else text
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("#", "//")):
            continue
        return line
    return ""


def _strip_prompt_prefix(line: str) -> str:
    line = line.strip()
    if line.startswith(("-", "*")):
        line = line[1:].strip()
    if line.startswith("$ "):
        line = line[2:].strip()
    return line


def _strip_shell_comment_outcome(command: str) -> tuple[str, str, str]:
    index = _unquoted_hash_index(command)
    if index < 0:
        return command.strip(), "", ""
    before = command[:index].rstrip()
    after = command[index + 1:].strip()
    tokens = _split_tokens(after)
    if len(tokens) == 1 and _clean_token(tokens[0]).lower() in _OUTCOME_TOKENS:
        return before, _clean_token(tokens[0]).lower(), "trailing comment outcome prose"
    return before, after, "trailing shell comment"


def _unquoted_hash_index(text: str) -> int:
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#" and (index == 0 or text[index - 1].isspace()):
            return index
    return -1


def _strip_trailing_outcome_token(command: str, tokens: list[str]) -> tuple[str, str] | None:
    if not tokens:
        return None
    last = _clean_token(tokens[-1]).lower()
    if last not in _OUTCOME_TOKENS:
        return None
    match = re.search(r"\s+(\S+)\s*$", command)
    if not match:
        return None
    if _clean_token(match.group(1)).lower() != last:
        return None
    return command[: match.start()].rstrip(), last


def _extract_cd_wrapper(command: str) -> tuple[str, str] | None:
    match = re.match(
        r"^\s*(?:cd|chdir)\s+(?P<cwd>\"[^\"]+\"|'[^']+'|[^&;]+?)\s*(?:&&|;)\s*(?P<command>.+?)\s*$",
        command,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    cwd = _clean_token(match.group("cwd"))
    inner_command = match.group("command").strip()
    if not cwd or not inner_command:
        return None
    return cwd, inner_command


def _extract_package_manager_cwd(command: str) -> tuple[str, str] | None:
    tokens = _split_tokens(command)
    if len(tokens) < 3:
        return None
    cleaned = [_clean_token(token) for token in tokens]
    manager = cleaned[0].lower()
    if manager.endswith(".cmd") or manager.endswith(".exe"):
        manager = manager.rsplit(".", 1)[0]
    if manager not in {"npm", "pnpm", "yarn"}:
        return None

    cwd_index: int | None = None
    if manager == "npm":
        for index, token in enumerate(cleaned[1:], start=1):
            if token in {"--prefix", "-C"}:
                cwd_index = index
                break
    elif manager == "pnpm":
        for index, token in enumerate(cleaned[1:], start=1):
            if token == "-C":
                cwd_index = index
                break
    elif manager == "yarn":
        for index, token in enumerate(cleaned[1:], start=1):
            if token == "--cwd":
                cwd_index = index
                break

    if cwd_index is None or cwd_index + 1 >= len(cleaned):
        return None

    cwd = cleaned[cwd_index + 1]
    rewritten = tokens[:cwd_index] + tokens[cwd_index + 2 :]
    if len(rewritten) < 2:
        return None
    return cwd, _join_tokens(rewritten)


def _looks_like_command(command: str) -> bool:
    tokens = _split_tokens(command)
    if not tokens:
        return False
    first = _clean_token(tokens[0]).lower().replace("\\", "/").rsplit("/", 1)[-1]
    if first.endswith(".exe"):
        first = first[:-4]
    is_godot = bool(re.match(r"^godot(?:4|[-_.].*)?$", first))
    return first in _KNOWN_COMMANDS or is_godot


def _split_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return command.split()


def _join_tokens(tokens: list[str]) -> str:
    try:
        return shlex.join([_clean_token(token) for token in tokens])
    except Exception:
        return " ".join(_clean_token(token) for token in tokens)


def _clean_token(token: str) -> str:
    return str(token).strip().strip("'\"'").strip()


def _is_pytest_tokens(tokens: list[str]) -> bool:
    cleaned = [_clean_token(token).lower().replace("\\", "/").rsplit("/", 1)[-1] for token in tokens]
    if not cleaned:
        return False
    if cleaned[0] in {"pytest", "pytest.exe"}:
        return True
    return len(cleaned) >= 3 and cleaned[1] == "-m" and cleaned[2] == "pytest"


def _contains_timeout(output: str) -> bool:
    lowered = output.lower()
    return "timed out" in lowered or "timeout" in lowered


def _is_missing_executable(lowered_output: str) -> bool:
    return any(
        marker in lowered_output
        for marker in (
            "command not found",
            "not recognized as an internal or external command",
            "is not recognized as the name of",
            "no such file or directory",
        )
    )


def _is_missing_dependency(lowered_output: str) -> bool:
    return (
        "no module named" in lowered_output
        or "modulenotfounderror" in lowered_output
        or "cannot find module" in lowered_output
    )


def _is_package_manifest_missing(tokens: list[str], lowered_output: str) -> bool:
    if not tokens:
        return False
    executable = _clean_token(tokens[0]).lower().replace("\\", "/").rsplit("/", 1)[-1]
    if executable.endswith((".cmd", ".exe")):
        executable = executable.rsplit(".", 1)[0]
    if executable not in {"npm", "pnpm", "yarn"}:
        return False
    markers = (
        "package.json",
        "no importer manifest",
        "enoent",
        "no package.json found",
        "couldn't find a package.json",
        "could not find a package.json",
    )
    if "package.json" not in lowered_output:
        return False
    return any(marker in lowered_output for marker in markers)


def _pytest_missing_path(output: str) -> str:
    match = re.search(r"ERROR:\s*file or directory not found:\s*([^\r\n]+)", output, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _pytest_no_tests_collected(lowered_output: str) -> bool:
    return "collected 0 items" in lowered_output or "no tests ran" in lowered_output


def _pytest_selection_empty(lowered_output: str) -> bool:
    return "0 selected" in lowered_output or "deselected" in lowered_output and "collected" in lowered_output
