"""One-time evaluator-owned automatic trial-count calibration."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .commands import render_command
from .comparison_run import (
    _validate_batch,
    _validate_prepare_response,
    _validate_subject_output,
)
from .errors import ProcessError, StateError
from .git import ensure_clean_worktree, resolve_commit
from .manifest import EvaluatorManifest
from .models import TaskConfig
from .process import run_or_load_once
from .sandbox import (
    command_runtime_read_paths,
    marked_command,
    sandbox_command,
    sanitized_environment,
)
from .seeds import derive_seed, new_master_seed
from .storage import write_json_once
from .trials import freeze_automatic_trial_count, load_trial_count

CalibrationCommandBuilder = Callable[
    [Sequence[str], Path, Sequence[Path], Sequence[Path], str],
    Sequence[str],
]
CalibrationProgress = Callable[[dict[str, Any]], None]


def _sandboxed(
    command: Sequence[str],
    cwd: Path,
    read_paths: Sequence[Path],
    write_paths: Sequence[Path],
    profile: str,
) -> Sequence[str]:
    return sandbox_command(
        command,
        cwd=cwd,
        read_paths=(*read_paths, *command_runtime_read_paths(command)),
        write_paths=write_paths,
        profile=profile,
    )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise StateError(f"{label} was not written to the reserved path")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise StateError(f"{label} must contain one JSON object")
    return value


def _validated_response(
    response: dict[str, Any],
    *,
    request: dict[str, Any],
    evaluator_commit: str,
    manifest_hash: str,
    policy: str,
    ceiling: int,
) -> int:
    fields = {
        "schema_version",
        "operation",
        "champion",
        "evaluator",
        "manifest",
        "policy",
        "recommended_trial_count",
        "criterion_met",
        "evidence",
    }
    count = response.get("recommended_trial_count")
    if (
        set(response) != fields
        or response.get("schema_version") != 1
        or response.get("operation") != "calibrate"
        or response.get("champion") != request["champion"]
        or response.get("evaluator") != evaluator_commit
        or response.get("manifest") != manifest_hash
        or response.get("policy") != policy
        or response.get("criterion_met") is not True
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count <= 0
        or count > ceiling
        or not isinstance(response.get("evidence"), dict)
        or not response["evidence"]
    ):
        raise StateError("calibration response violates the approved contract")
    return count


def _notify(
    progress: CalibrationProgress | None,
    stage: str,
    status: str,
    **fields: Any,
) -> None:
    if progress is not None:
        progress(
            {
                "event": "stage",
                "scope": "calibration",
                "stage": stage,
                "status": status,
                **fields,
            }
        )


def _run_pilot_process(
    directory: Path,
    command: Sequence[str],
    *,
    cwd: Path,
    read_paths: Sequence[Path],
    write_paths: Sequence[Path],
    profile: str,
    source: str,
    manifest: EvaluatorManifest,
    command_builder: CalibrationCommandBuilder,
    codex_home: Path,
    writable_home: Path,
    stop_path: Path,
    execution_marker: Path,
) -> None:
    managed = command_builder(
        marked_command(command, execution_marker),
        cwd,
        (*read_paths, *command_runtime_read_paths(command)),
        write_paths,
        profile,
    )
    try:
        result = run_or_load_once(
            directory,
            managed,
            timeout_seconds=manifest.limits.timeout_seconds,
            max_output_bytes=manifest.limits.max_output_bytes,
            cwd=cwd,
            env=sanitized_environment(
                codex_home=codex_home,
                writable_home=writable_home,
            ),
            stop_path=stop_path,
        )
    except (ProcessError, StateError) as error:
        raise StateError(f"calibration {source} failed and cannot be retried") from error
    if result["return_code"] != 0:
        if not execution_marker.is_file():
            raise StateError(
                f"calibration {source} sandbox did not start its reserved command"
            )
        raise StateError(
            f"calibration {source} exited unsuccessfully and cannot be retried"
        )


def _pilot_selection(
    response: dict[str, Any],
    *,
    request: dict[str, Any],
    manifest: EvaluatorManifest,
    evaluator_commit: str,
    manifest_hash: str,
) -> tuple[int, dict[str, Any]]:
    expected = {
        "schema_version",
        "operation",
        "champion",
        "evaluator",
        "manifest",
        "policy",
        "assessments",
    }
    if (
        set(response) != expected
        or response["schema_version"] != 2
        or response["operation"] != "calibrate"
        or response["champion"] != request["champion"]
        or response["evaluator"] != evaluator_commit
        or response["manifest"] != manifest_hash
        or response["policy"] != manifest.calibration.policy
        or not isinstance(response["assessments"], list)
    ):
        raise StateError("calibration response violates the controller-pilot contract")
    assessments = response["assessments"]
    values: list[float] = []
    counts: list[int] = []
    for assessment in assessments:
        if (
            not isinstance(assessment, dict)
            or set(assessment) != {"trial_count", "diagnostic_value"}
        ):
            raise StateError(
                "calibration response violates the controller-pilot contract"
            )
        count = assessment["trial_count"]
        value = assessment["diagnostic_value"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise StateError("calibration assessment values are invalid")
        counts.append(count)
        values.append(float(value))
    if counts != list(manifest.calibration.ladder):
        raise StateError("calibration assessments differ from the approved ladder")
    maximum = manifest.calibration.diagnostic_maximum
    if maximum is None:
        raise StateError("controller-pilot calibration has no approved maximum")
    passes = [value <= maximum for value in values]
    selected_index = next(
        (
            index
            for index in range(len(passes))
            if all(passes[index:])
        ),
        len(passes) - 1,
    )
    criterion_met = passes[-1]
    return counts[selected_index], {
        "criterion_met": criterion_met,
        "diagnostic": manifest.calibration.diagnostic_name,
        "units": manifest.calibration.diagnostic_units,
        "maximum": maximum,
        "selected_value": values[selected_index],
        "ceiling_fallback": not criterion_met,
    }


def _calibrate_controller_pilot(
    task_directory: Path,
    task: TaskConfig,
    manifest: EvaluatorManifest,
    *,
    manifest_hash: str,
    evaluator_commit: str,
    evaluator_directory: Path,
    champion_directory: Path,
    stop_path: Path,
    command_builder: CalibrationCommandBuilder,
    progress: CalibrationProgress | None,
) -> int:
    calibration = manifest.calibration
    if (
        not calibration.controller_pilot
        or not calibration.ladder
        or calibration.ceiling is None
        or calibration.policy is None
    ):
        raise StateError("approved evaluator has no controller-run pilot")
    champion = resolve_commit(
        task.repo,
        f"refs/arctl/{task.task_id}/champion",
    )
    if resolve_commit(champion_directory, "HEAD") != champion:
        raise StateError("calibration champion differs from the approved champion")
    ensure_clean_worktree(champion_directory)

    root = task_directory / "calibration"
    requests = root / "requests"
    outputs = root / "outputs"
    prepare_output = outputs / "prepare"
    champion_output = outputs / "champion"
    assessment_output = outputs / "assessment"
    for path in (prepare_output, champion_output, assessment_output):
        path.mkdir(parents=True, exist_ok=True)
    codex_home = root / "sandbox-home"
    codex_home.mkdir(exist_ok=True)

    reservation_path = root / "request.private.json"
    if reservation_path.exists():
        reservation = _load_object(reservation_path, "calibration reservation")
    else:
        master = new_master_seed()
        reservation = {
            "schema_version": 2,
            "operation": "calibrate",
            "champion": champion,
            "evaluator": evaluator_commit,
            "manifest": manifest_hash,
            "policy": calibration.policy,
            "seed_derivation": "arctl-seed-v1",
            "master_seed": master.hex(),
            "trial_seeds": [
                derive_seed(
                    master,
                    experiment_id=0,
                    phase="calibration",
                    subject="evaluator",
                    trial=index,
                )
                for index in range(calibration.ceiling)
            ],
            "ladder": list(calibration.ladder),
            "diagnostic": {
                "name": calibration.diagnostic_name,
                "units": calibration.diagnostic_units,
                "maximum": calibration.diagnostic_maximum,
            },
        }
        write_json_once(reservation_path, reservation)
    expected_fields = {
        "schema_version",
        "operation",
        "champion",
        "evaluator",
        "manifest",
        "policy",
        "seed_derivation",
        "master_seed",
        "trial_seeds",
        "ladder",
        "diagnostic",
    }
    try:
        master = bytes.fromhex(reservation["master_seed"])
    except (KeyError, TypeError, ValueError) as error:
        raise StateError("saved calibration master seed is invalid") from error
    expected_seeds = [
        derive_seed(
            master,
            experiment_id=0,
            phase="calibration",
            subject="evaluator",
            trial=index,
        )
        for index in range(calibration.ceiling)
    ]
    if (
        set(reservation) != expected_fields
        or reservation["schema_version"] != 2
        or reservation["operation"] != "calibrate"
        or reservation["champion"] != champion
        or reservation["evaluator"] != evaluator_commit
        or reservation["manifest"] != manifest_hash
        or reservation["policy"] != calibration.policy
        or reservation["seed_derivation"] != "arctl-seed-v1"
        or len(master) != 32
        or reservation["trial_seeds"] != expected_seeds
        or reservation["ladder"] != list(calibration.ladder)
        or reservation["diagnostic"]
        != {
            "name": calibration.diagnostic_name,
            "units": calibration.diagnostic_units,
            "maximum": calibration.diagnostic_maximum,
        }
    ):
        raise StateError("saved calibration reservation differs from the approval")

    completed_path = task_directory / "calibration.private.json"
    if (task_directory / "trial-count.json").is_file():
        completed = _load_object(completed_path, "calibration evidence")
        if (
            set(completed) != {"request", "response"}
            or completed["request"] != reservation
            or not isinstance(completed["response"], dict)
        ):
            raise StateError("saved calibration evidence differs from the approval")
        count, _ = _pilot_selection(
            completed["response"],
            request=reservation,
            manifest=manifest,
            evaluator_commit=evaluator_commit,
            manifest_hash=manifest_hash,
        )
        if load_trial_count(task_directory, task) != count:
            raise StateError("frozen trial count differs from calibration evidence")
        return count

    _notify(progress, "reserve", "complete", trial_count=calibration.ceiling)
    batch_path = prepare_output / "batch.public.json"
    scoring_path = prepare_output / "scoring.private.json"
    prepare_request = requests / "prepare.json"
    prepare_response = prepare_output / "response.json"
    write_json_once(
        prepare_request,
        {
            "schema_version": 1,
            "operation": "prepare",
            "kind": "calibration",
            "experiment_id": 0,
            "trial_count": calibration.ceiling,
            "trial_seeds": reservation["trial_seeds"],
            "public_batch": str(batch_path.resolve()),
            "private_scoring": str(scoring_path.resolve()),
        },
    )
    prepare_command = render_command(
        manifest.prepare_command,
        {"request": prepare_request, "response": prepare_response},
        allowed_roots=(root,),
    )
    _notify(progress, "prepare", "started", trial_count=calibration.ceiling)
    _run_pilot_process(
        root / "process" / "prepare",
        prepare_command,
        cwd=evaluator_directory,
        read_paths=(evaluator_directory, prepare_request),
        write_paths=(prepare_output,),
        profile="arctl-evaluator",
        source="prepare",
        manifest=manifest,
        command_builder=command_builder,
        codex_home=codex_home,
        writable_home=prepare_output,
        stop_path=stop_path,
        execution_marker=prepare_output / "execution.started",
    )
    _notify(progress, "prepare", "complete", trial_count=calibration.ceiling)
    _validate_prepare_response(
        prepare_response,
        kind="calibration",
        trial_count=calibration.ceiling,
    )
    _validate_batch(
        batch_path,
        trial_count=calibration.ceiling,
        manifest=manifest,
    )
    if scoring_path.is_symlink() or not scoring_path.is_file():
        raise StateError("calibration private scoring data is missing")

    subject_result = champion_output / "result.json"
    subject_command = render_command(
        manifest.subject_command,
        {"input": batch_path, "output": subject_result},
        allowed_roots=(root,),
    )
    _notify(
        progress,
        "champion_pilot",
        "started",
        trial_count=calibration.ceiling,
    )
    _run_pilot_process(
        root / "process" / "champion",
        subject_command,
        cwd=champion_directory,
        read_paths=(
            champion_directory,
            batch_path,
        ),
        write_paths=(champion_output,),
        profile="arctl-subject",
        source="champion pilot",
        manifest=manifest,
        command_builder=command_builder,
        codex_home=codex_home,
        writable_home=champion_output,
        stop_path=stop_path,
        execution_marker=champion_output / "execution.started",
    )
    _notify(
        progress,
        "champion_pilot",
        "complete",
        trial_count=calibration.ceiling,
    )
    _validate_subject_output(
        subject_result,
        subject="champion",
        trial_count=calibration.ceiling,
        manifest=manifest,
    )

    assessment_request = requests / "calibrate.json"
    assessment_response = assessment_output / "response.private.json"
    write_json_once(
        assessment_request,
        {
            **{
                key: reservation[key]
                for key in (
                    "schema_version",
                    "operation",
                    "champion",
                    "evaluator",
                    "manifest",
                    "policy",
                    "ladder",
                    "diagnostic",
                )
            },
            "champion_output": str(subject_result.resolve()),
        },
    )
    assessment_command = render_command(
        manifest.calibrate_command or (),
        {"request": assessment_request, "response": assessment_response},
        allowed_roots=(root,),
    )
    _notify(progress, "assessment", "started")
    _run_pilot_process(
        root / "process" / "assessment",
        assessment_command,
        cwd=evaluator_directory,
        read_paths=(evaluator_directory, assessment_request, subject_result),
        write_paths=(assessment_output,),
        profile="arctl-evaluator",
        source="assessment",
        manifest=manifest,
        command_builder=command_builder,
        codex_home=codex_home,
        writable_home=assessment_output,
        stop_path=stop_path,
        execution_marker=assessment_output / "execution.started",
    )
    response = _load_object(assessment_response, "calibration assessment")
    count, summary = _pilot_selection(
        response,
        request=reservation,
        manifest=manifest,
        evaluator_commit=evaluator_commit,
        manifest_hash=manifest_hash,
    )
    _notify(progress, "assessment", "complete")
    write_json_once(completed_path, {"request": reservation, "response": response})
    freeze_automatic_trial_count(
        task_directory,
        task,
        count,
        calibration=summary,
    )
    _notify(
        progress,
        "freeze",
        "complete",
        trial_count=count,
        criterion_met=summary["criterion_met"],
    )
    return count


def calibrate_trial_count(
    task_directory: Path,
    task: TaskConfig,
    manifest: EvaluatorManifest,
    *,
    manifest_hash: str,
    evaluator_commit: str,
    evaluator_directory: Path,
    champion_directory: Path | None = None,
    stop_path: Path,
    command_builder: CalibrationCommandBuilder = _sandboxed,
    progress: CalibrationProgress | None = None,
) -> int:
    """Run or recover the approved calibration exactly once."""
    if task.trials != "auto":
        return load_trial_count(task_directory, task)
    command_template = manifest.calibrate_command
    ceiling = manifest.calibration.ceiling
    policy = manifest.calibration.policy
    if command_template is None or ceiling is None or policy is None:
        raise StateError("approved evaluator does not support automatic calibration")
    if (
        resolve_commit(evaluator_directory, "HEAD") != evaluator_commit
        or resolve_commit(task.evaluator.repo, task.evaluator.commit) != evaluator_commit
    ):
        raise StateError("calibration evaluator differs from the approved commit")
    ensure_clean_worktree(evaluator_directory)
    if manifest.calibration.controller_pilot:
        if champion_directory is None:
            raise StateError("controller-run calibration has no champion checkout")
        return _calibrate_controller_pilot(
            task_directory,
            task,
            manifest,
            manifest_hash=manifest_hash,
            evaluator_commit=evaluator_commit,
            evaluator_directory=evaluator_directory,
            champion_directory=champion_directory,
            stop_path=stop_path,
            command_builder=command_builder,
            progress=progress,
        )
    champion = resolve_commit(
        task.repo,
        f"refs/arctl/{task.task_id}/champion",
    )

    root = task_directory / "calibration"
    output = root / "output"
    output.mkdir(parents=True, exist_ok=True)
    sandbox_home = root / "sandbox-home"
    sandbox_home.mkdir(exist_ok=True)
    request_path = root / "request.private.json"
    response_path = output / "response.private.json"
    if request_path.exists():
        request = _load_object(request_path, "calibration request")
    else:
        master = new_master_seed()
        request = {
            "schema_version": 1,
            "operation": "calibrate",
            "champion": champion,
            "evaluator": evaluator_commit,
            "manifest": manifest_hash,
            "policy": policy,
            "seed_derivation": "arctl-seed-v1",
            "master_seed": master.hex(),
            "trial_seeds": [
                derive_seed(
                    master,
                    experiment_id=0,
                    phase="calibration",
                    subject="evaluator",
                    trial=index,
                )
                for index in range(ceiling)
            ],
            "ceiling": ceiling,
        }
        write_json_once(request_path, request)
    expected_request_fields = {
        "schema_version",
        "operation",
        "champion",
        "evaluator",
        "manifest",
        "policy",
        "seed_derivation",
        "master_seed",
        "trial_seeds",
        "ceiling",
    }
    try:
        master = bytes.fromhex(request["master_seed"])
    except (KeyError, TypeError, ValueError) as error:
        raise StateError("saved calibration master seed is invalid") from error
    if len(master) != 32:
        raise StateError("saved calibration master seed is invalid")
    expected_seeds = [
        derive_seed(
            master,
            experiment_id=0,
            phase="calibration",
            subject="evaluator",
            trial=index,
        )
        for index in range(ceiling)
    ]
    if (
        set(request) != expected_request_fields
        or request["schema_version"] != 1
        or request["operation"] != "calibrate"
        or request["champion"] != champion
        or request["evaluator"] != evaluator_commit
        or request["manifest"] != manifest_hash
        or request["policy"] != policy
        or request["seed_derivation"] != "arctl-seed-v1"
        or request["ceiling"] != ceiling
        or not isinstance(request["trial_seeds"], list)
        or request["trial_seeds"] != expected_seeds
    ):
        raise StateError("saved calibration request differs from the approval")

    completed_path = task_directory / "calibration.private.json"
    if (task_directory / "trial-count.json").is_file():
        completed = _load_object(completed_path, "calibration evidence")
        if set(completed) != {"request", "response"} or completed["request"] != request:
            raise StateError("saved calibration evidence differs from the approval")
        response = completed["response"]
        if not isinstance(response, dict):
            raise StateError("saved calibration response must contain one JSON object")
        count = _validated_response(
            response,
            request=request,
            evaluator_commit=evaluator_commit,
            manifest_hash=manifest_hash,
            policy=policy,
            ceiling=ceiling,
        )
        frozen = load_trial_count(task_directory, task)
        if frozen != count:
            raise StateError("frozen trial count differs from calibration evidence")
        return frozen

    command = render_command(
        command_template,
        {"request": request_path, "response": response_path},
        allowed_roots=(task_directory,),
    )
    managed = command_builder(
        command,
        evaluator_directory,
        (evaluator_directory, request_path),
        (output,),
        "arctl-evaluator",
    )
    try:
        result = run_or_load_once(
            root / "process",
            managed,
            timeout_seconds=manifest.limits.timeout_seconds,
            max_output_bytes=manifest.limits.max_output_bytes,
            cwd=evaluator_directory,
            env=sanitized_environment(
                codex_home=sandbox_home,
                writable_home=output,
            ),
            stop_path=stop_path,
        )
    except (ProcessError, StateError) as error:
        raise StateError("automatic calibration failed and cannot be retried") from error
    if result["return_code"] != 0:
        raise StateError("automatic calibration exited unsuccessfully and cannot be retried")
    response = _load_object(response_path, "calibration response")
    count = _validated_response(
        response,
        request=request,
        evaluator_commit=evaluator_commit,
        manifest_hash=manifest_hash,
        policy=policy,
        ceiling=ceiling,
    )
    write_json_once(
        task_directory / "calibration.private.json",
        {"request": request, "response": response},
    )
    freeze_automatic_trial_count(task_directory, task, count)
    frozen = load_trial_count(task_directory, task)
    if frozen != count:
        raise StateError("frozen trial count differs from calibration evidence")
    return frozen
