"""Task discovery from the data directory and current repository."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import StateError
from .models import TaskConfig, validate_task_id
from .taskio import load_task


@dataclass(frozen=True)
class LocatedTask:
    directory: Path
    config: TaskConfig


def locate_task(
    data_root: Path,
    *,
    task_id: str | None,
    current_directory: Path,
) -> LocatedTask:
    tasks_root = data_root / "tasks"
    if task_id is not None:
        validate_task_id(task_id)
        directory = tasks_root / task_id
        task_file = directory / "task.yaml"
        if not task_file.is_file():
            raise StateError(f"task does not exist: {task_id}")
        task = load_task(task_file)
        if task.task_id != task_id:
            raise StateError("task directory and task_id do not match")
        return LocatedTask(directory, task)

    current = current_directory.resolve()
    matches: list[LocatedTask] = []
    if tasks_root.is_dir():
        for task_file in sorted(tasks_root.glob("*/task.yaml")):
            task = load_task(task_file)
            repo = task.repo.resolve()
            if current == repo or repo in current.parents:
                matches.append(LocatedTask(task_file.parent, task))
    if not matches:
        raise StateError("no task matches the current repository")
    if len(matches) > 1:
        identifiers = ", ".join(match.config.task_id for match in matches)
        raise StateError(f"multiple tasks match; choose one explicitly: {identifiers}")
    return matches[0]
