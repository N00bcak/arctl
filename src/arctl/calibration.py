"""One-time evaluator-owned automatic trial-count calibration."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .commands import render_command
from .errors import ProcessError, StateError
from .git import ensure_clean_worktree, resolve_commit
from .manifest import EvaluatorManifest
from .models import TaskConfig
from .process import run_or_load_once
from .sandbox import command_runtime_read_paths, sandbox_command, sanitized_environment
from .seeds import derive_seed, new_master_seed
from .storage import write_json_once
from .trials import freeze_automatic_trial_count, load_trial_count

CalibrationCommandBuilder = Callable[
    [Sequence[str], Path, Sequence[Path], Sequence[Path], str],
    Sequence[str],
]


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


def calibrate_trial_count(
    task_directory: Path,
    task: TaskConfig,
    manifest: EvaluatorManifest,
    *,
    manifest_hash: str,
    evaluator_commit: str,
    evaluator_directory: Path,
    stop_path: Path,
    command_builder: CalibrationCommandBuilder = _sandboxed,
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
