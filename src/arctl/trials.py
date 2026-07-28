"""Frozen trial-count records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .errors import StateError
from .models import TaskConfig
from .storage import write_json_once

_FIELDS = {"schema_version", "source", "trial_count"}


def freeze_fixed_trial_count(task_directory: Path, task: TaskConfig) -> None:
    if task.trials == "auto":
        return
    write_json_once(
        task_directory / "trial-count.json",
        {
            "schema_version": 1,
            "source": "fixed",
            "trial_count": task.trials,
        },
    )


def freeze_automatic_trial_count(
    task_directory: Path,
    task: TaskConfig,
    trial_count: int,
) -> None:
    if task.trials != "auto":
        raise StateError("cannot save automatic calibration for a fixed task")
    if isinstance(trial_count, bool) or not isinstance(trial_count, int) or trial_count <= 0:
        raise StateError("automatic trial count must be a positive integer")
    write_json_once(
        task_directory / "trial-count.json",
        {
            "schema_version": 1,
            "source": "automatic",
            "trial_count": trial_count,
        },
    )


def load_trial_count(task_directory: Path, task: TaskConfig) -> int:
    try:
        value: Any = json.loads(
            (task_directory / "trial-count.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        if task.trials == "auto":
            raise StateError("automatic trial calibration has not completed") from error
        raise StateError("fixed trial-count record is missing") from error
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise StateError("trial-count record fields are invalid")
    count = value["trial_count"]
    if (
        value["schema_version"] != 1
        or value["source"] not in ("fixed", "automatic")
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count <= 0
    ):
        raise StateError("trial-count record values are invalid")
    if task.trials != "auto" and (
        value["source"] != "fixed" or count != task.trials
    ):
        raise StateError("fixed trial-count record differs from the approved task")
    if task.trials == "auto" and value["source"] != "automatic":
        raise StateError("automatic task has a non-automatic trial-count record")
    return count
