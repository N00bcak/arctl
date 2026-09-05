"""Frozen trial-count records."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from .errors import StateError
from .models import TaskConfig
from .storage import write_json_once

_FIELDS = {"source", "trial_count"}
_CALIBRATION_FIELDS = {
    "criterion_met",
    "diagnostic",
    "units",
    "maximum",
    "selected_value",
    "ceiling_fallback",
}


def freeze_fixed_trial_count(task_directory: Path, task: TaskConfig) -> None:
    if task.trials == "auto":
        return
    write_json_once(
        task_directory / "trial-count.json",
        {
            "source": "fixed",
            "trial_count": task.trials,
        },
    )


def freeze_automatic_trial_count(
    task_directory: Path,
    task: TaskConfig,
    trial_count: int,
    *,
    calibration: Mapping[str, Any] | None = None,
) -> None:
    if task.trials != "auto":
        raise StateError("cannot save automatic calibration for a fixed task")
    if isinstance(trial_count, bool) or not isinstance(trial_count, int) or trial_count <= 0:
        raise StateError("automatic trial count must be a positive integer")
    value: dict[str, Any] = {
        "source": "automatic",
        "trial_count": trial_count,
    }
    if calibration is not None:
        if set(calibration) != _CALIBRATION_FIELDS:
            raise StateError("automatic calibration summary fields are invalid")
        value["calibration"] = dict(calibration)
    write_json_once(task_directory / "trial-count.json", value)


def load_trial_count_record(
    task_directory: Path,
    task: TaskConfig,
) -> dict[str, Any]:
    try:
        value: Any = json.loads(
            (task_directory / "trial-count.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        if task.trials == "auto":
            raise StateError("automatic trial calibration has not completed") from error
        raise StateError("fixed trial-count record is missing") from error
    if (
        not isinstance(value, Mapping)
        or set(value) not in (_FIELDS, _FIELDS | {"calibration"})
    ):
        raise StateError("trial-count record fields are invalid")
    count = value["trial_count"]
    if (
        value["source"] not in ("fixed", "automatic")
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
    if "calibration" in value:
        if value["source"] != "automatic":
            raise StateError("calibrated trial-count record must be automatic")
        calibration = value["calibration"]
        if not isinstance(calibration, Mapping) or set(calibration) != _CALIBRATION_FIELDS:
            raise StateError("automatic calibration summary fields are invalid")
        maximum = calibration["maximum"]
        selected = calibration["selected_value"]
        if (
            not isinstance(calibration["criterion_met"], bool)
            or not isinstance(calibration["ceiling_fallback"], bool)
            or not isinstance(calibration["diagnostic"], str)
            or not calibration["diagnostic"]
            or not isinstance(calibration["units"], str)
            or not calibration["units"]
            or isinstance(maximum, bool)
            or not isinstance(maximum, (int, float))
            or isinstance(selected, bool)
            or not isinstance(selected, (int, float))
            or not math.isfinite(float(maximum))
            or maximum < 0
            or not math.isfinite(float(selected))
            or selected < 0
            or calibration["ceiling_fallback"]
            == calibration["criterion_met"]
        ):
            raise StateError("automatic calibration summary values are invalid")
    return dict(value)


def load_trial_count(task_directory: Path, task: TaskConfig) -> int:
    return int(load_trial_count_record(task_directory, task)["trial_count"])
