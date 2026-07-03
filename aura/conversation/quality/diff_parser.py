from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class DiffFile:
    path: str
    added: list[tuple[int | None, str]] = field(default_factory=list)
    removed: list[tuple[int | None, str]] = field(default_factory=list)
    new_file: bool = False

    @property
    def changed_line_count(self) -> int:
        return len(self.added) + len(self.removed)


def parse_unified_diff(diff_text: str) -> dict[str, DiffFile]:
    files: dict[str, DiffFile] = {}
    current: DiffFile | None = None
    next_removed_line: int | None = None
    next_added_line: int | None = None

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            current = None
            next_removed_line = None
            next_added_line = None
            parts = raw_line.split()
            if len(parts) >= 4:
                path = parts[3]
                if path.startswith("b/"):
                    path = path[2:]
                current = files.setdefault(path, DiffFile(path=path))
            continue
        if current is None:
            continue
        if raw_line == "new file mode" or raw_line.startswith("new file mode "):
            current.new_file = True
            continue
        if raw_line.startswith("--- /dev/null"):
            current.new_file = True
            continue
        if raw_line.startswith("+++ "):
            path = raw_line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path and path != "/dev/null" and path != current.path:
                files.pop(current.path, None)
                current.path = path
                files[path] = current
            continue
        if raw_line.startswith("@@"):
            next_removed_line = _parse_hunk_start(raw_line, "-")
            next_added_line = _parse_hunk_start(raw_line, "+")
            continue
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        if raw_line.startswith("+"):
            text = raw_line[1:]
            current.added.append((next_added_line, text))
            if next_added_line is not None:
                next_added_line += 1
            continue
        if raw_line.startswith("-"):
            text = raw_line[1:]
            current.removed.append((next_removed_line, text))
            if next_removed_line is not None:
                next_removed_line += 1
            continue
        if raw_line.startswith(" "):
            if next_removed_line is not None:
                next_removed_line += 1
            if next_added_line is not None:
                next_added_line += 1

    return files


def _parse_hunk_start(line: str, prefix: str) -> int | None:
    match = re.search(rf"\{prefix}(\d+)(?:,\d+)?", line)
    if not match:
        return None
    return int(match.group(1))
