"""Safe status, report, inspect, and stop views over task artifacts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .dossier import ensure_experiment_dossier
from .errors import ArctlError, StateError
from .experiment import ExperimentRecord, load_experiment
from .registry import LocatedTask
from .results import validate_public_result
from .storage import atomic_write_json
from .taskio import load_manifest
from .trials import load_trial_count, load_trial_count_record


def _experiment_directories(task: LocatedTask) -> list[Path]:
    root = task.directory / "experiments"
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and path.name.isdigit()),
        key=lambda path: int(path.name),
    )


def _public_result(task: LocatedTask, directory: Path) -> dict[str, Any] | None:
    path = directory / "result.public.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"public result is invalid: {path}") from error
    try:
        manifest, _ = load_manifest(task.directory / "evaluator.manifest.json")
        return validate_public_result(
            value,
            allowed_telemetry=manifest.public_telemetry,
            allowed_suspect_reasons=manifest.suspect_reason_codes,
            expected_statistic=manifest.public_statistic,
        )
    except ArctlError as error:
        raise StateError(f"public result is invalid: {path}") from error


def task_status(task: LocatedTask) -> dict[str, Any]:
    approved = (task.directory / "approval.json").is_file()
    trial_count: int | None = None
    calibration_summary: dict[str, Any] | None = None
    calibration = "not_started" if task.config.trials == "auto" else "fixed"
    if approved:
        try:
            trial_count = load_trial_count(task.directory, task.config)
            trial_record = load_trial_count_record(task.directory, task.config)
            calibration_summary = trial_record.get("calibration")
            calibration = "complete" if task.config.trials == "auto" else "fixed"
        except StateError:
            if task.config.trials != "auto":
                raise
            calibration_processes = task.directory / "calibration" / "process"
            if (
                (calibration_processes / "started.json").is_file()
                or any(calibration_processes.glob("*/started.json"))
            ):
                calibration = "failed"

    directories = _experiment_directories(task)
    latest: ExperimentRecord | None = None
    last_result: dict[str, Any] | None = None
    for directory in directories:
        record = load_experiment(directory)
        latest = record
        result = _public_result(task, directory)
        if result is not None:
            last_result = result

    strategies = sorted((task.directory / "strategy").glob("*.public.json"))
    strategy_failures = sorted(
        (task.directory / "strategy").glob(
            "[0-9][0-9][0-9][0-9][0-9][0-9]/strategy.failure.json"
        )
    )
    searches = sorted((task.directory / "searches").glob("[0-9]" * 6))
    latest_search = searches[-1] if searches else None
    search_stalled = (
        latest_search is not None
        and (latest_search / "stalled.public.json").is_file()
        and (latest is None or latest.state == "COMPLETE")
    )
    attempts = (
        sorted((latest_search / "attempts").glob("[0-9][0-9]"))
        if latest_search is not None
        else []
    )
    search_research_failed = bool(
        attempts and (attempts[-1] / "research.failure.json").is_file()
    )

    strategy_failed = bool(
        strategy_failures
        and int(strategy_failures[-1].parent.name)
        > max(
            (int(path.name.split(".")[0]) for path in strategies),
            default=0,
        )
    )
    reflection_attempts = (
        sorted((directories[-1] / "reflection" / "attempts").glob("[0-9]" * 4))
        if directories
        else []
    )
    reflection_failed = bool(
        latest is not None
        and latest.state == "REFLECTING"
        and (
            (directories[-1] / "reflection.blocked.json").is_file()
            or (
                reflection_attempts
                and (reflection_attempts[-1] / "reflection.failure.json").is_file()
            )
        )
    )
    if reflection_failed:
        state = "REFLECTION_FAILED"
    elif strategy_failed:
        state = "STRATEGY_FAILED"
    elif search_research_failed:
        state = "RESEARCH_FAILED"
    elif (
        latest is not None
        and (directories[-1] / "research.failure.json").is_file()
    ):
        state = "RESEARCH_FAILED"
    elif (
        latest is not None
        and (directories[-1] / "public-check.failure.json").is_file()
    ):
        state = "PUBLIC_CHECK_FAILED"
    elif latest is not None and latest.state != "COMPLETE":
        state = latest.state
    elif search_stalled:
        state = "SEARCH_STALLED"
    elif approved and trial_count is not None:
        state = "READY"
    elif approved and calibration == "failed":
        state = "CALIBRATION_FAILED"
    elif approved:
        state = "CALIBRATION_REQUIRED"
    else:
        state = "TASK_DRAFT"
    champion_ref = f"refs/arctl/{task.config.task_id}/champion"
    champion = None
    if approved:
        from .git import resolve_commit

        champion = resolve_commit(task.config.repo, champion_ref)
    return {
        "state": state,
        "approved": approved,
        "calibration": calibration,
        "trial_count": trial_count,
        "calibration_summary": calibration_summary,
        "experiment_id": latest.experiment_id if latest is not None else None,
        "champion": champion,
        "provisional": latest is not None and latest.state in (
            "PROVISIONAL",
            "SUSPECT_RESERVED",
        ),
        "last_result": last_result,
        "stop_requested": (task.directory / "stop.requested").exists(),
        "strategy_revision": len(strategies),
        "search_id": int(latest_search.name) if latest_search is not None else None,
        "search_attempt": len(attempts) if attempts else None,
        "log_path": (
            str(
                reflection_attempts[-1] / "process"
                if reflection_attempts
                else directories[-1] / "reflection"
            )
            if reflection_failed
            else str(strategy_failures[-1].parent / "process")
            if strategy_failed
            else str(attempts[-1] / "process")
            if search_research_failed
            else str(directories[-1] / "process")
            if directories
            else str(task.directory)
        ),
    }


def exploration_history(
    task: LocatedTask,
    *,
    query: str | None = None,
    path: str | None = None,
    decision: str | None = None,
) -> dict[str, Any]:
    from .search import search_ledger

    entries = search_ledger(
        task.directory,
        query=query,
        path=path,
        decision=decision,
    )
    return {
        "task_id": task.config.task_id,
        "entries": entries,
        "count": len(entries),
        "ledger_path": str(task.directory / "exploration" / "ledger.public.jsonl"),
    }


def task_report(task: LocatedTask) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for directory in _experiment_directories(task):
        if not (directory / "published").is_file():
            continue
        result = _public_result(task, directory)
        if result is None:
            continue
        dossier = ensure_experiment_dossier(
            task.directory,
            task.config,
            directory,
            result,
        )
        results.append({**result, "dossier_path": str(dossier)})
    calibration_summary = None
    if (task.directory / "trial-count.json").is_file():
        calibration_summary = load_trial_count_record(
            task.directory, task.config
        ).get("calibration")
    return {
        "task_id": task.config.task_id,
        "completed_experiments": len(results),
        "results": results,
        "calibration_summary": calibration_summary,
        "limitations": (
            "Uncertainty is calculated by the approved evaluator for each candidate "
            "comparison. arctl validates the protocol and evidence shape, not the "
            "evaluator's mathematics. Calibration and suspect testing are best-effort "
            "mitigations; this adaptive search has no task-wide multiple-testing "
            "correction or harness-wide false-promotion guarantee."
        ),
    }


def inspect_experiment(
    task: LocatedTask,
    experiment_id: int | None,
) -> dict[str, Any]:
    directories = _experiment_directories(task)
    if experiment_id is None:
        active = [
            directory
            for directory in directories
            if load_experiment(directory).state != "COMPLETE"
        ]
        if len(active) == 1:
            directory = active[0]
        elif len(directories) == 1:
            directory = directories[0]
        else:
            raise StateError("choose an experiment ID explicitly")
    else:
        if experiment_id <= 0:
            raise StateError("experiment ID must be positive")
        directory = task.directory / "experiments" / f"{experiment_id:06d}"
        if not directory.is_dir():
            raise StateError(f"experiment does not exist: {experiment_id}")
    record = load_experiment(directory)
    result = _public_result(task, directory)
    dossier = (
        ensure_experiment_dossier(
            task.directory,
            task.config,
            directory,
            result,
        )
        if result is not None
        else None
    )
    artifacts = [
        {
            "path": str(path.relative_to(directory)),
            "visibility": (
                "public"
                if path.name
                in {
                    "request.public.json",
                    "result.public.json",
                    "reflection.public.json",
                    "published",
                }
                else "private"
            ),
        }
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]
    return {
        "experiment": record.to_json(),
        "result": result,
        "dossier_path": str(dossier) if dossier is not None else None,
        "artifacts": artifacts,
        "calibration_summary": (
            load_trial_count_record(task.directory, task.config).get("calibration")
            if (task.directory / "trial-count.json").is_file()
            else None
        ),
    }


def request_stop(task: LocatedTask) -> bool:
    path = task.directory / "stop.requested"
    if path.exists():
        return False
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "requested_at_unix": time.time(),
        },
    )
    return True
