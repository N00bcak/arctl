"""Safe status, report, inspect, and stop views over task artifacts."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .dossier import ensure_experiment_dossier, rebuild_task_index
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


def _latest_agent_process(root: Path) -> Path:
    attempts = sorted((root / "attempts").glob("[0-9]" * 4))
    return attempts[-1] / "process"


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


def _champion_provenance(
    task: LocatedTask,
    commit: str | None,
) -> dict[str, Any] | None:
    if commit is None:
        return None
    # A commit may be promoted again after a later rollback. Attribute the current
    # champion to the most recent promotion, not merely its first appearance.
    for directory in reversed(_experiment_directories(task)):
        if not (directory / "published").is_file():
            continue
        result = _public_result(task, directory)
        if (
            result is not None
            and result["decision"] == "ACCEPT"
            and result["candidate"] == commit
            and result["champion_after"] == commit
        ):
            return {
                "kind": "experiment",
                "experiment_id": result["experiment_id"],
                "hypothesis": result["hypothesis"],
            }
    try:
        approval = json.loads(
            (task.directory / "approval.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        approval = None
    if isinstance(approval, dict) and approval.get("champion") == commit:
        return {"kind": "initial", "experiment_id": None, "hypothesis": None}
    return {"kind": "unknown", "experiment_id": None, "hypothesis": None}


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
    completed_experiments = sum(
        load_experiment(directory).state == "COMPLETE"
        and (directory / "published").is_file()
        for directory in directories
    )
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
    attempts = (
        sorted(
            path
            for path in (latest_search / "attempts").iterdir()
            if path.is_dir() and path.name.isdigit()
        )
        if latest_search is not None and (latest_search / "attempts").is_dir()
        else []
    )
    search_research_failed = bool(
        attempts and (attempts[-1] / "research.failure.json").is_file()
    )
    planning_failed = bool(
        attempts and (attempts[-1] / "planning.failure.json").is_file()
    )
    implementation_failed = bool(
        attempts and (attempts[-1] / "implementation.failure.json").is_file()
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
    elif planning_failed:
        state = "PLANNING_FAILED"
    elif implementation_failed:
        state = "IMPLEMENTATION_FAILED"
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
    elif (
        approved
        and task.config.max_experiments is not None
        and completed_experiments >= task.config.max_experiments
    ):
        state = "LIMIT_REACHED"
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
    gc_journal = task.directory / ".gc" / "transaction.json"
    mini_gc_path = task.directory / ".gc" / "mini-gc-failure.json"
    mini_gc_failure: dict[str, Any] | None = None
    if mini_gc_path.is_file():
        try:
            value = json.loads(mini_gc_path.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or set(value)
                != {"experiment_id", "phase", "reason", "plan_hash"}
                or isinstance(value.get("experiment_id"), bool)
                or not isinstance(value.get("experiment_id"), int)
                or value["experiment_id"] <= 0
                or value.get("phase") not in {"planning", "execution", "recovery"}
                or not isinstance(value.get("reason"), str)
                or not value["reason"]
                or (
                    value.get("plan_hash") is not None
                    and not isinstance(value["plan_hash"], str)
                )
            ):
                raise ValueError("invalid mini-GC failure record")
            mini_gc_failure = value
        except (OSError, json.JSONDecodeError, ValueError):
            mini_gc_failure = {
                "experiment_id": None,
                "phase": "unknown",
                "reason": "experiment cleanup failure record is unreadable",
                "plan_hash": None,
            }
    gc_pending = gc_journal.is_file() or mini_gc_failure is not None
    gc_errors: list[str] = []
    if gc_journal.is_file():
        try:
            gc_record = json.loads(gc_journal.read_text(encoding="utf-8"))
            errors = gc_record.get("errors")
            if isinstance(errors, Mapping):
                gc_errors = sorted(
                    {value for value in errors.values() if isinstance(value, str)}
                )
        except (OSError, json.JSONDecodeError):
            gc_errors = ["cleanup journal is unreadable"]
    if mini_gc_failure is not None:
        gc_errors = sorted({*gc_errors, mini_gc_failure["reason"]})
    return {
        "state": state,
        "approved": approved,
        "calibration": calibration,
        "trial_count": trial_count,
        "completed_experiments": completed_experiments,
        "max_experiments": task.config.max_experiments,
        "calibration_summary": calibration_summary,
        "experiment_id": latest.experiment_id if latest is not None else None,
        "champion": champion,
        "champion_provenance": _champion_provenance(task, champion),
        "provisional": latest is not None and latest.state in (
            "PROVISIONAL",
            "SUSPECT_RESERVED",
        ),
        "last_result": last_result,
        "stop_requested": (task.directory / "stop.requested").exists(),
        "gc_pending": gc_pending,
        "gc_errors": gc_errors,
        "mini_gc_failure": mini_gc_failure,
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
            else str(
                _latest_agent_process(
                    strategy_failures[-1].parent,
                )
            )
            if strategy_failed
            else str(
                _latest_agent_process(attempts[-1] / "planning")
            )
            if planning_failed
            else str(
                _latest_agent_process(attempts[-1] / "implementation")
            )
            if implementation_failed
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
    index = rebuild_task_index(task.directory, results)
    return {
        "task_id": task.config.task_id,
        "completed_experiments": len(results),
        "results": results,
        "dossier_root": str(task.directory / "reports" / "experiments"),
        "index_path": str(index),
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
                if path.name.endswith(".public.json")
                or path.name in {
                    "request.public.json",
                    "planning.public.json",
                    "implementation.public.json",
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
        "champion_after_provenance": _champion_provenance(
            task,
            result["champion_after"] if result is not None else None,
        ),
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
            "requested_at_unix": time.time(),
        },
    )
    return True
