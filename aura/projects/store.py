from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aura.paths import data_dir
from aura.projects.models import ProjectSpace, ProjectThread


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid4().hex[:12]


class ProjectStore:
    def __init__(self) -> None:
        self._data_dir: Path = data_dir() / "projects"
        self._index_path: Path = self._data_dir / "index.json"

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
        self._index_path.write_text(
            json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def list_projects(self, include_archived: bool = False) -> list[ProjectSpace]:
        index = self._load_index()
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
