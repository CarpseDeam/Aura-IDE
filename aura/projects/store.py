from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aura.paths import data_dir
from aura.projects.models import ProjectSpace, ProjectThread


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid4().hex[:12]


def _full_clean_thread_title(text: str) -> str:
    if not text:
        return "Conversation"

    # Step 1: Splitting text into lines
    lines = text.splitlines()

    # Regex patterns
    timestamp_pattern = re.compile(r'^\d{4}[-/]\d{2}[-/]\d{2}')
    log_level_pattern = re.compile(r'\b(ERROR|WARN|INFO|DEBUG)\b')
    pytest_pattern = re.compile(r'\b\d+\s+(?:passed|failed|error|errors|skipped)\b')
    bullet_pattern = re.compile(r'^(?:[-*•]\s+|\[[xX]\]\s+|\d+[\.)]\s+)')

    cleaned_line = None
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if "```" in stripped or "~~~" in stripped:
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        if stripped.startswith("/") or re.match(r'^[a-zA-Z]:[/\\]', stripped):
            parts = stripped.split(maxsplit=1)
            stripped = parts[1].strip() if len(parts) > 1 else ""

        if not stripped:
            continue

        # Noise checks:
        # Date-prefixed timestamps
        if timestamp_pattern.search(stripped):
            continue

        # Log levels
        if log_level_pattern.search(stripped):
            continue

        # Pytest summary lines
        if pytest_pattern.search(stripped):
            continue

        # <30% alphabetic chars
        alpha_count = sum(c.isalpha() for c in stripped)
        if (alpha_count / len(stripped)) < 0.3:
            continue

        # Found our line!
        cleaned_line = stripped
        break

    if cleaned_line is None:
        return "Conversation"

    # Step 4: Stripping leading bullets/numbering repeatedly
    while True:
        new_stripped = bullet_pattern.sub('', cleaned_line)
        if new_stripped == cleaned_line:
            break
        cleaned_line = new_stripped

    # Step 5: Stripping markdown bold/italic markers
    cleaned_line = re.sub(r'\*\*(.*?)\*\*', r'\1', cleaned_line)
    cleaned_line = re.sub(r'\*(.*?)\*', r'\1', cleaned_line)

    # Step 6: Stripping leading # markers
    cleaned_line = re.sub(r'^#+\s*', '', cleaned_line)

    # Step 7: Collapsing whitespace
    cleaned_line = " ".join(cleaned_line.split())

    # Step 8: Stripping excessive trailing punctuation
    cleaned_line = re.sub(r'[.,!?;:]{2,}$', '', cleaned_line)

    # Step 9: If nothing remains
    if not cleaned_line:
        return "Conversation"

    return cleaned_line


def _clean_thread_title(text: str, max_len: int = 72) -> str:
    cleaned = _full_clean_thread_title(text)
    if len(cleaned) <= max_len:
        return cleaned

    truncated = cleaned[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len * 0.5:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "..."


class ProjectStore:
    def __init__(self) -> None:
        self._data_dir: Path = data_dir() / "projects"
        self._index_path: Path = self._data_dir / "index.json"
        self.repair_index()

    @staticmethod
    def _canonical_root(root_path: Path) -> str:
        """Return a case-normalized absolute path string for identity matching."""
        resolved = root_path.expanduser().resolve()
        return os.path.normcase(str(resolved))

    @staticmethod
    def clean_thread_title(text: str, max_len: int = 72) -> str:
        return _clean_thread_title(text, max_len)

    def _load_index(self) -> dict:
        if not self._index_path.exists():
            return {}
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save_index(self, index: dict) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_projects(self, include_archived: bool = False) -> list[ProjectSpace]:
        index = self._load_index()

        # Deduplicate by canonical root path
        canonical_map: dict[str, str] = {}  # canonical_root -> pid
        for pid, entry in index.items():
            root_path_str = entry.get("root_path") if isinstance(entry, dict) else None
            if not root_path_str:
                continue
            canonical = self._canonical_root(Path(root_path_str))
            if canonical not in canonical_map:
                canonical_map[canonical] = pid

        # Save cleaned index if duplicates were removed
        if len(canonical_map) != len(index):
            cleaned = {}
            for pid, entry in index.items():
                root_path_str = entry.get("root_path") if isinstance(entry, dict) else None
                if not root_path_str:
                    continue
                if self._canonical_root(Path(root_path_str)) in canonical_map:
                    canonical_pid = canonical_map[self._canonical_root(Path(root_path_str))]
                    if pid == canonical_pid:
                        cleaned[pid] = entry
            self._save_index(cleaned)
            index = cleaned

        projects: list[ProjectSpace] = []
        for pid, entry in index.items():
            root_path_str = entry.get("root_path") if isinstance(entry, dict) else None
            if not root_path_str:
                continue
            project = self._load_project_from_root(Path(root_path_str))
            if project is None:
                continue
            if not include_archived and project.archived:
                continue
            projects.append(project)
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        return projects

    def create_or_update_project(self, root_path: Path, name: str | None = None) -> ProjectSpace:
        metadata_path = root_path / ".aura" / "project.json"
        if metadata_path.exists():
            project = self._load_project_from_root(root_path)
            if project is not None:
                if name is not None:
                    project.name = name
                project.updated_at = _utc_iso()
                self.save_project(project)
                self._prune_stale_index_entries(root_path, project.id)
                return project

        now = _utc_iso()
        project = ProjectSpace(
            id=_new_id(),
            name=name if name is not None else root_path.name,
            root_path=root_path,
            created_at=now,
            updated_at=now,
        )
        self.save_project(project)
        self._prune_stale_index_entries(root_path, project.id)
        return project

    def load_project(self, project_id: str) -> ProjectSpace | None:
        index = self._load_index()
        entry = index.get(project_id)
        if not isinstance(entry, dict):
            return None
        root_path_str = entry.get("root_path")
        if not root_path_str:
            return None
        return self._load_project_from_root(Path(root_path_str))

    def save_project(self, project: ProjectSpace) -> None:
        project.updated_at = _utc_iso()
        metadata_path = project.root_path / ".aura" / "project.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(project.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        index = self._load_index()
        index[project.id] = {
            "root_path": project.root_path.as_posix(),
            "name": project.name,
        }
        self._save_index(index)

    def _prune_stale_index_entries(self, root_path: Path, keep_id: str) -> None:
        """Remove index entries with the same canonical root but a different ID."""
        index = self._load_index()
        canonical = self._canonical_root(root_path)
        changed = False
        for pid in list(index.keys()):
            if pid == keep_id:
                continue
            entry = index[pid]
            if not isinstance(entry, dict):
                continue
            entry_root = entry.get("root_path")
            if not entry_root:
                continue
            if self._canonical_root(Path(entry_root)) == canonical:
                del index[pid]
                changed = True
        if changed:
            self._save_index(index)

    def repair_index(self) -> None:
        """Remove duplicate, stale, or missing-root entries from the index."""
        index = self._load_index()
        cleaned: dict[str, dict] = {}
        for pid, entry in list(index.items()):
            root_path_str = entry.get("root_path") if isinstance(entry, dict) else None
            if not root_path_str:
                continue
            root_path = Path(root_path_str)
            if not root_path.is_dir():
                continue
            project = self._load_project_from_root(root_path)
            if project is None:
                continue
            canonical = self._canonical_root(root_path)
            existing_pid = None
            for cpid, centry in cleaned.items():
                if self._canonical_root(Path(centry.get("root_path", ""))) == canonical:
                    existing_pid = cpid
                    break
            if existing_pid is None:
                cleaned[pid] = entry
        if len(cleaned) != len(index):
            self._save_index(cleaned)

    def list_threads(self, project: ProjectSpace, include_archived: bool = False) -> list[ProjectThread]:
        threads_dir = project.root_path / ".aura" / "threads"
        if not threads_dir.is_dir():
            return []
        threads: list[ProjectThread] = []
        for path in sorted(threads_dir.iterdir()):
            if not path.suffix == ".json":
                continue
            thread = self._load_thread_from_path(path)
            if thread is None:
                continue
            if not include_archived and thread.archived:
                continue
            threads.append(thread)
        threads.sort(key=lambda t: t.updated_at, reverse=True)
        return threads

    def create_thread(self, project: ProjectSpace, title: str = "New thread") -> ProjectThread:
        title = self.clean_thread_title(title)
        now = _utc_iso()
        thread = ProjectThread(
            id=_new_id(),
            project_id=project.id,
            title=title,
            conversation_path=None,
            created_at=now,
            updated_at=now,
        )
        self.save_thread(project, thread)
        project.last_thread_id = thread.id
        self.save_project(project)
        return thread

    def load_thread(self, project: ProjectSpace, thread_id: str) -> ProjectThread | None:
        path = project.root_path / ".aura" / "threads" / f"{thread_id}.json"
        return self._load_thread_from_path(path)

    def save_thread(self, project: ProjectSpace, thread: ProjectThread) -> None:
        thread.updated_at = _utc_iso()
        threads_dir = project.root_path / ".aura" / "threads"
        threads_dir.mkdir(parents=True, exist_ok=True)
        path = threads_dir / f"{thread.id}.json"
        path.write_text(
            json.dumps(thread.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def touch_thread(self, project: ProjectSpace, thread_id: str, conversation_path: Path | None = None) -> None:
        thread = self.load_thread(project, thread_id)
        if thread is None:
            return
        thread.updated_at = _utc_iso()
        if conversation_path is not None:
            thread.conversation_path = conversation_path
        self.save_thread(project, thread)

    def backfill_threads_from_conversations(
        self, project: ProjectSpace, max_title_length: int = 72
    ) -> list[ProjectThread]:
        """
        Scan .aura/conversations/*.json files and create ProjectThread entries
        for any conversation file that doesn't already have a thread pointing at it.
        Returns the list of newly created threads.
        Does NOT move or rename conversation files.
        Does NOT create duplicate threads (checks by conversation_path).
        """
        conv_dir = project.root_path / ".aura" / "conversations"
        if not conv_dir.is_dir():
            return []

        # Build set of existing conversation_paths
        existing_paths = set()
        for t in self.list_threads(project, include_archived=True):
            if t.conversation_path is not None:
                existing_paths.add(t.conversation_path.resolve())

        new_threads = []
        for conv_file in sorted(conv_dir.iterdir()):
            if not conv_file.suffix == ".json":
                continue
            # Skip thread metadata files (stored in .aura/threads/ not .aura/conversations/)
            resolved = conv_file.resolve()
            if resolved in existing_paths:
                continue

            # Derive title from first user message or filename
            display_title, full_title = self._derive_title_from_conversation(conv_file, max_title_length)

            thread = ProjectThread(
                id=_new_id(),
                project_id=project.id,
                title=display_title,
                summary=full_title,
                conversation_path=resolved,
                created_at=_utc_iso(),
                updated_at=_utc_iso(),
            )
            self.save_thread(project, thread)
            new_threads.append(thread)
            existing_paths.add(resolved)

        if new_threads:
            project.last_thread_id = new_threads[0].id
            self.save_project(project)

        return new_threads

    @staticmethod
    def _derive_title_from_conversation(path: Path, max_len: int = 72) -> tuple[str, str]:
        """Read a conversation JSON file and extract a clean title from the first user message."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _clean_thread_title(path.stem, max_len), _full_clean_thread_title(path.stem)
        if not isinstance(data, dict):
            return _clean_thread_title(path.stem, max_len), _full_clean_thread_title(path.stem)

        msgs = data.get("messages", [])
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return _clean_thread_title(content, max_len), _full_clean_thread_title(content)
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if text.strip():
                            return _clean_thread_title(text, max_len), _full_clean_thread_title(text)
        # Fallback: use filename stem
        return _clean_thread_title(path.stem, max_len), _full_clean_thread_title(path.stem)

    @staticmethod
    def _load_project_from_root(root_path: Path) -> ProjectSpace | None:
        metadata_path = root_path / ".aura" / "project.json"
        if not metadata_path.exists():
            return None
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return ProjectSpace.from_dict(data)

    @staticmethod
    def _load_thread_from_path(path: Path) -> ProjectThread | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return ProjectThread.from_dict(data)
