"""Focused file-selection prompt helpers for Aura actions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONTEXT_LINES = 40
MAX_SELECTION_CHARS = 12_000


class FocusedActionError(ValueError):
    """Raised when a focused action prompt cannot be built safely."""


class AmbiguousSelectionError(FocusedActionError):
    """Raised when selected text appears multiple times without offsets."""


@dataclass(frozen=True)
class SelectionContext:
    relative_path: str
    selected_text: str
    context_text: str
    start_line: int
    end_line: int
    context_start_line: int
    context_end_line: int
    language: str
    selection_truncated: bool = False
    whole_file: bool = False


ACTION_LABELS: dict[str, str] = {
    "ask": "Ask Aura about selection",
    "explain": "Explain selected code",
    "fix": "Fix selected code",
    "refactor": "Refactor selected code",
    "simplify": "Simplify selected code",
    "add_logging": "Add logging to selection",
    "add_type_hints": "Add type hints to selection",
    "write_tests": "Write tests for selection",
}

EDIT_ACTIONS = {
    "fix",
    "refactor",
    "simplify",
    "add_logging",
    "add_type_hints",
    "write_tests",
}


def is_edit_action(action_key: str) -> bool:
    return action_key in EDIT_ACTIONS


def language_from_path(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".html": "html",
        ".svg": "svg",
        ".md": "markdown",
        ".py": "python",
        ".pyi": "python",
        ".gd": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "jsx",
        ".css": "css",
        ".scss": "scss",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".rs": "rust",
        ".go": "go",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".java": "java",
        ".kt": "kotlin",
        ".swift": "swift",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".txt": "text",
        ".cfg": "ini",
        ".ini": "ini",
        ".xml": "xml",
        ".sql": "sql",
        ".r": "r",
    }.get(ext, "text")


def extract_selection_context(
    relative_path: str,
    full_file_text: str,
    selected_text: str,
    selection_start_offset: int | None = None,
    selection_end_offset: int | None = None,
    context_lines: int = DEFAULT_CONTEXT_LINES,
    max_selection_chars: int = MAX_SELECTION_CHARS,
) -> SelectionContext:
    """Compute line numbers and surrounding context for a selected region."""
    whole_file = False
    if not selected_text:
        selected_text = full_file_text
        selection_start_offset = 0
        selection_end_offset = len(full_file_text)
        whole_file = True

    if selection_start_offset is None or selection_end_offset is None:
        selection_start_offset, selection_end_offset = _find_unique_selection(
            full_file_text, selected_text
        )

    start = max(0, min(selection_start_offset, len(full_file_text)))
    end = max(start, min(selection_end_offset, len(full_file_text)))
    selected_text = full_file_text[start:end] or selected_text

    start_line = _line_at_offset(full_file_text, start)
    end_line = _line_at_offset(full_file_text, max(start, end - 1))

    lines = full_file_text.splitlines()
    if not lines and full_file_text:
        lines = [full_file_text]
    if not lines:
        lines = [""]

    half = max(1, context_lines // 2)
    context_start_line = max(1, start_line - half)
    context_end_line = min(len(lines), end_line + half)
    context_slice = lines[context_start_line - 1 : context_end_line]
    context_text = _number_lines(context_slice, context_start_line)

    selection_truncated = len(selected_text) > max_selection_chars
    if selection_truncated:
        selected_text = (
            selected_text[:max_selection_chars]
            + "\n\n[Selection truncated because it is very large.]"
        )

    return SelectionContext(
        relative_path=relative_path,
        selected_text=selected_text,
        context_text=context_text,
        start_line=start_line,
        end_line=end_line,
        context_start_line=context_start_line,
        context_end_line=context_end_line,
        language=language_from_path(relative_path),
        selection_truncated=selection_truncated,
        whole_file=whole_file,
    )


def build_ask_selection_prompt(
    relative_path: str,
    full_file_text: str,
    selected_text: str,
    question: str,
    selection_start_offset: int | None = None,
    selection_end_offset: int | None = None,
    read_only_mode: bool = False,
) -> str:
    return _build_selection_prompt(
        "Ask Aura about selection",
        relative_path,
        full_file_text,
        selected_text,
        selection_start_offset,
        selection_end_offset,
        custom_request=question,
        read_only_mode=read_only_mode,
    )


def build_explain_selection_prompt(
    relative_path: str,
    full_file_text: str,
    selected_text: str,
    selection_start_offset: int | None = None,
    selection_end_offset: int | None = None,
    read_only_mode: bool = False,
) -> str:
    return _build_selection_prompt(
        "Explain selected code",
        relative_path,
        full_file_text,
        selected_text,
        selection_start_offset,
        selection_end_offset,
        read_only_mode=read_only_mode,
        force_read_only=True,
    )


def build_fix_selection_prompt(*args, **kwargs) -> str:
    return _build_named_edit_prompt("Fix selected code", *args, **kwargs)


def build_refactor_selection_prompt(*args, **kwargs) -> str:
    return _build_named_edit_prompt("Refactor selected code", *args, **kwargs)


def build_simplify_selection_prompt(*args, **kwargs) -> str:
    return _build_named_edit_prompt("Simplify selected code", *args, **kwargs)


def build_add_logging_selection_prompt(*args, **kwargs) -> str:
    return _build_named_edit_prompt("Add logging to selection", *args, **kwargs)


def build_add_type_hints_selection_prompt(*args, **kwargs) -> str:
    return _build_named_edit_prompt("Add type hints to selection", *args, **kwargs)


def build_write_tests_for_selection_prompt(*args, **kwargs) -> str:
    return _build_named_edit_prompt("Write tests for selection", *args, **kwargs)


def build_prompt_for_action(
    action_key: str,
    relative_path: str,
    full_file_text: str,
    selected_text: str,
    selection_start_offset: int | None = None,
    selection_end_offset: int | None = None,
    custom_question: str = "",
    read_only_mode: bool = False,
) -> str:
    builders = {
        "ask": build_ask_selection_prompt,
        "explain": build_explain_selection_prompt,
        "fix": build_fix_selection_prompt,
        "refactor": build_refactor_selection_prompt,
        "simplify": build_simplify_selection_prompt,
        "add_logging": build_add_logging_selection_prompt,
        "add_type_hints": build_add_type_hints_selection_prompt,
        "write_tests": build_write_tests_for_selection_prompt,
    }
    try:
        builder = builders[action_key]
    except KeyError as exc:
        raise FocusedActionError(f"Unknown focused action: {action_key}") from exc
    if action_key == "ask":
        return builder(
            relative_path,
            full_file_text,
            selected_text,
            custom_question,
            selection_start_offset,
            selection_end_offset,
            read_only_mode,
        )
    return builder(
        relative_path,
        full_file_text,
        selected_text,
        selection_start_offset,
        selection_end_offset,
        read_only_mode=read_only_mode,
    )


def _build_named_edit_prompt(
    action: str,
    relative_path: str,
    full_file_text: str,
    selected_text: str,
    selection_start_offset: int | None = None,
    selection_end_offset: int | None = None,
    read_only_mode: bool = False,
) -> str:
    return _build_selection_prompt(
        action,
        relative_path,
        full_file_text,
        selected_text,
        selection_start_offset,
        selection_end_offset,
        read_only_mode=read_only_mode,
    )


def _build_selection_prompt(
    action: str,
    relative_path: str,
    full_file_text: str,
    selected_text: str,
    selection_start_offset: int | None,
    selection_end_offset: int | None,
    custom_request: str = "",
    read_only_mode: bool = False,
    force_read_only: bool = False,
) -> str:
    ctx = extract_selection_context(
        relative_path,
        full_file_text,
        selected_text,
        selection_start_offset,
        selection_end_offset,
    )
    requested_action = action
    if custom_request:
        requested_action = f"{action}: {custom_request}"

    instructions = [
        "Operate on the selected code region first.",
        "Do not rewrite unrelated parts of the file.",
        "If a broader change is necessary, explain why before making it.",
        "Prefer minimal diffs.",
        "Preserve existing style and project conventions.",
    ]
    read_only = force_read_only or read_only_mode
    if read_only:
        instructions.append("This is a read-only request: do not modify files.")
        instructions.append("Provide explanation or suggested changes only.")
    else:
        instructions.append(
            "Use existing Aura file-edit tools and normal diff approval for any modification."
        )
        instructions.append(
            "For existing-file modifications, read the file and use patch_file with exact replacement hunks; use write_file only for new files or intentional full-file replacement."
        )

    if ctx.selection_truncated:
        instructions.append(
            "The selected text was truncated in this prompt; ask for more context before editing omitted parts."
        )
    if ctx.whole_file:
        instructions.append(
            "No explicit text selection was provided, so this request falls back to the whole current file."
        )

    instruction_block = "\n".join(f"* {line}" for line in instructions)
    return f"""Focused code action request.

File:
{ctx.relative_path}

Selected lines:
{ctx.start_line}-{ctx.end_line}

Requested action:
{requested_action}

Selected code:
```{ctx.language}
{ctx.selected_text}
```

Surrounding context (lines {ctx.context_start_line}-{ctx.context_end_line}):
```{ctx.language}
{ctx.context_text}
```

Instructions:
{instruction_block}
"""


def _find_unique_selection(full_file_text: str, selected_text: str) -> tuple[int, int]:
    first = full_file_text.find(selected_text)
    if first < 0:
        raise FocusedActionError("Selected text was not found in the file.")
    second = full_file_text.find(selected_text, first + len(selected_text))
    if second >= 0:
        raise AmbiguousSelectionError(
            "Selected text appears multiple times; select a larger region."
        )
    return first, first + len(selected_text)


def _line_at_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _number_lines(lines: list[str], start_line: int) -> str:
    return "\n".join(
        f"{line_no:>4}: {line}" for line_no, line in enumerate(lines, start=start_line)
    )
