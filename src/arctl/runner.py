"""Persistent single-task experiment orchestration."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .approval import verify_approval
from .calibration import CalibrationCommandBuilder, calibrate_trial_count
from .comparison import ComparisonReservation, load_reservation, reserve_comparison
from .comparison_run import CommandBuilder, ComparisonFailure, run_comparison
from .errors import ArctlError, ProcessError, StateError, StoppedError, ValidationError
from .experiment import (
    freeze_candidate,
    load_experiment,
    load_research_request,
    mark_comparison_reserved,
    publish_comparison_failure,
    publish_final_result,
    run_public_checks,
    save_comparison_result,
    start_experiment,
)
from .git import (
    create_detached_worktree,
    delete_ref,
    ensure_clean_worktree,
    remove_worktree,
    resolve_commit,
)
from .manifest import EvaluatorManifest
from .models import Evidence
from .process import run_or_load_once
from .registry import LocatedTask
from .results import validate_public_result
from .sandbox import research_command, sanitized_environment
from .storage import write_json_once
from .taskio import load_manifest
from .trials import load_trial_count

ResearchCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]
PublicCheckCommandBuilder = Callable[[Sequence[str], Path, Path], Sequence[str]]

def _validate_codex_output_schema(schema: Mapping[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise StateError("Codex output schema is not valid JSON Schema") from error

    def visit(node: Mapping[str, Any], path: str) -> None:
        if "$ref" not in node and "anyOf" not in node and "type" not in node:
            raise StateError(f"Codex output schema node lacks a type: {path}")
        alternatives = node.get("anyOf")
        if alternatives is not None:
            if not isinstance(alternatives, list):
                raise StateError(f"Codex output schema anyOf is invalid: {path}")
            for index, alternative in enumerate(alternatives):
                if not isinstance(alternative, Mapping):
                    raise StateError(f"Codex output schema anyOf is invalid: {path}")
                visit(alternative, f"{path}.anyOf[{index}]")
        if node.get("type") == "object":
            properties = node.get("properties")
            required = node.get("required")
            if (
                not isinstance(properties, Mapping)
                or node.get("additionalProperties") is not False
                or not isinstance(required, list)
                or len(required) != len(properties)
                or set(required) != set(properties)
            ):
                raise StateError(f"Codex output schema object is not strict: {path}")
            for name, child in properties.items():
                if not isinstance(name, str) or not isinstance(child, Mapping):
                    raise StateError(
                        f"Codex output schema properties are invalid: {path}"
                    )
                visit(child, f"{path}.properties.{name}")
        if node.get("type") == "array":
            items = node.get("items")
            if not isinstance(items, Mapping):
                raise StateError(f"Codex output schema array lacks items: {path}")
            visit(items, f"{path}.items")
        definitions = node.get("$defs", {})
        if not isinstance(definitions, Mapping):
            raise StateError(f"Codex output schema definitions are invalid: {path}")
        for name, child in definitions.items():
            if not isinstance(name, str) or not isinstance(child, Mapping):
                raise StateError(f"Codex output schema definitions are invalid: {path}")
            visit(child, f"{path}.$defs.{name}")

    if schema.get("type") != "object":
        raise StateError("Codex output schema root must be an object")
    visit(schema, "$")


def _research_schema(manifest: EvaluatorManifest) -> dict[str, Any]:
    telemetry = {
        name: {"type": ["string", "null"], "minLength": 1}
        for name in manifest.public_telemetry
    }
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "claim",
            "mechanism",
            "expected_effect",
            "expected_telemetry",
            "falsifiers",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "claim": {"type": "string", "minLength": 1},
            "mechanism": {"type": "string", "minLength": 1},
            "expected_effect": {"type": "string", "minLength": 1},
            "expected_telemetry": {
                "type": "object",
                "additionalProperties": False,
                "required": list(telemetry),
                "properties": telemetry,
            },
            "falsifiers": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }
    _validate_codex_output_schema(schema)
    return schema


@dataclass(frozen=True)
class RunOutcome:
    results: tuple[dict[str, Any], ...]
    stopped: bool


def _default_research_command(
    worktree: Path,
    scratch: Path,
    schema: Path,
    prompt: str,
) -> Sequence[str]:
    return research_command(
        worktree=worktree,
        scratch=scratch,
        output_schema=schema,
        prompt=prompt,
    )


def _checkout(repo: Path, path: Path, revision: str) -> None:
    if not path.exists():
        create_detached_worktree(repo, path, revision)
    elif resolve_commit(path, "HEAD") != resolve_commit(repo, revision):
        raise StateError(f"saved worktree points at the wrong commit: {path}")
    ensure_clean_worktree(path)


def _public_history(
    task: LocatedTask,
    manifest: EvaluatorManifest,
) -> list[dict[str, Any]]:
    experiments = task.directory / "experiments"
    if not experiments.is_dir():
        return []
    history: list[dict[str, Any]] = []
    for path in sorted(experiments.glob("[0-9]" * 6)):
        result = path / "result.public.json"
        published = path / "published"
        if not result.is_file() or not published.is_file():
            continue
        try:
            value = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"published result is invalid: {result}") from error
        try:
            history.append(
                validate_public_result(
                    value,
                    allowed_telemetry=manifest.public_telemetry,
                    allowed_suspect_reasons=manifest.suspect_reason_codes,
                    expected_statistic=manifest.public_statistic,
                )
            )
        except StateError as error:
            raise StateError(f"published result is invalid: {result}") from error
    return history


def _research_prompt(task: LocatedTask, manifest: EvaluatorManifest) -> str:
    packet = {
        "objective": task.config.objective,
        "editable_paths": list(task.config.editable_paths),
        "denied_paths": list(task.config.denied_paths),
        "public_checks": [list(command) for command in task.config.public_checks],
        "public_probe": list(task.config.public_probe),
        "statistic": manifest.public_statistic,
        "subject_interface": manifest.subject_interface,
        "telemetry": list(manifest.public_telemetry),
        "completed_results": _public_history(task, manifest),
    }
    return (
        "Make one focused improvement to the checked-out champion for this public "
        "research task. Stay within editable_paths and do not commit. Run public "
        "checks or the public probe when useful. Your final response must be only "
        "the required research-request JSON object.\n\n"
        + json.dumps(packet, sort_keys=True, separators=(",", ":"))
    )


def _run_research(
    task: LocatedTask,
    experiment: Path,
    worktree: Path,
    manifest: EvaluatorManifest,
    *,
    command_builder: ResearchCommandBuilder,
    stop_path: Path,
) -> None:
    scratch = experiment / "research"
    scratch.mkdir(parents=True, exist_ok=True)
    schema = scratch / "request.schema.json"
    write_json_once(schema, _research_schema(manifest))
    command = command_builder(
        worktree,
        scratch,
        schema,
        _research_prompt(task, manifest),
    )
    if command_builder is _default_research_command:
        codex_home = Path(
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        )
        environment = sanitized_environment(
            codex_home=codex_home,
            writable_home=scratch,
        )
    else:
        environment = None
    try:
        try:
            result = run_or_load_once(
                experiment / "process" / "research",
                command,
                timeout_seconds=3600,
                max_output_bytes=2_000_000,
                cwd=worktree,
                env=environment,
                stop_path=stop_path,
            )
        except StoppedError:
            raise
        except (ProcessError, StateError) as error:
            raise StateError("fresh research session did not complete") from error
        if result["return_code"] != 0:
            raise StateError("fresh research session exited unsuccessfully")
        request_path = scratch / "request.public.json"
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(
                "fresh research session did not write valid request JSON"
            ) from error
        if not isinstance(request, dict):
            raise StateError("fresh research request must contain one JSON object")
        telemetry = request.get("expected_telemetry")
        if isinstance(telemetry, dict):
            request["expected_telemetry"] = {
                name: expectation
                for name, expectation in telemetry.items()
                if expectation is not None
            }
    except StateError as error:
        write_json_once(
            experiment / "research.failure.json",
            {
                "schema_version": 1,
                "message": str(error),
            },
        )
        raise
    write_json_once(experiment / "request.public.json", request)


def _reservation(
    path: Path,
    *,
    kind: str,
    experiment_id: int,
    champion: str,
    candidate: str,
    evaluator: str,
    manifest_hash: str,
    trial_count: int,
    manifest: EvaluatorManifest,
    excluded_seeds: set[int],
) -> ComparisonReservation:
    expected_commands = {
        "subject": manifest.subject_command,
        "prepare": manifest.prepare_command,
        "score": manifest.score_command,
    }
    if path.exists():
        saved = load_reservation(path)
        expected = (
            kind,
            experiment_id,
            champion,
            candidate,
            evaluator,
            manifest_hash,
            trial_count,
            expected_commands,
        )
        actual = (
            saved.kind,
            saved.experiment_id,
            saved.champion,
            saved.candidate,
            saved.evaluator,
            saved.manifest,
            saved.trial_count,
            dict(saved.commands),
        )
        if actual != expected:
            raise StateError("saved comparison reservation differs from the experiment")
        return saved
    return reserve_comparison(
        path,
        kind=kind,  # type: ignore[arg-type]
        experiment_id=experiment_id,
        champion=champion,
        candidate=candidate,
        evaluator=evaluator,
        manifest=manifest_hash,
        trial_count=trial_count,
        commands=expected_commands,
        excluded_seeds=excluded_seeds,
    )


def _run_reserved_comparison(
    task: LocatedTask,
    experiment: Path,
    *,
    kind: str,
    manifest: EvaluatorManifest,
    manifest_hash: str,
    evaluator_commit: str,
    trial_count: int,
    champion_worktree: Path,
    candidate_worktree: Path,
    evaluator_worktree: Path,
    command_builder: CommandBuilder | None,
    stop_path: Path,
):
    record_path = experiment / "comparisons" / kind / "reservation.private.json"
    record = load_experiment(experiment)
    if record.candidate is None:
        raise StateError("comparison requires a frozen candidate")
    reservation = _reservation(
        record_path,
        kind=kind,
        experiment_id=record.experiment_id,
        champion=record.champion,
        candidate=record.candidate,
        evaluator=evaluator_commit,
        manifest_hash=manifest_hash,
        trial_count=trial_count,
        manifest=manifest,
        excluded_seeds=_reserved_seeds(task.directory, excluding=record_path),
    )
    expected_state = "CANDIDATE_FROZEN" if kind == "primary" else "PROVISIONAL"
    if record.state == expected_state:
        mark_comparison_reserved(
            experiment,
            kind=kind,  # type: ignore[arg-type]
        )
    arguments: dict[str, Any] = {}
    if command_builder is not None:
        arguments["command_builder"] = command_builder
    return run_comparison(
        record_path.parent,
        reservation,
        manifest,
        manifest_hash=manifest_hash,
        evaluator_directory=evaluator_worktree,
        champion_directory=champion_worktree,
        candidate_directory=candidate_worktree,
        stop_path=stop_path,
        **arguments,
    )


def _reserved_seeds(task_directory: Path, *, excluding: Path) -> set[int]:
    seeds: set[int] = set()
    calibration = task_directory / "calibration.private.json"
    if calibration.is_file():
        try:
            value = json.loads(calibration.read_text(encoding="utf-8"))
            calibration_seeds = value["request"]["trial_seeds"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise StateError("saved calibration evidence is invalid") from error
        if not isinstance(calibration_seeds, list) or any(
            isinstance(seed, bool) or not isinstance(seed, int)
            for seed in calibration_seeds
        ):
            raise StateError("saved calibration evidence is invalid")
        seeds.update(calibration_seeds)
    experiments = task_directory / "experiments"
    if experiments.is_dir():
        for path in experiments.glob("*/comparisons/*/reservation.private.json"):
            if path == excluding:
                continue
            seeds.update(load_reservation(path).trial_seeds)
    return seeds


def _active_experiment(task_directory: Path) -> Path | None:
    experiments = task_directory / "experiments"
    if not experiments.is_dir():
        return None
    active = [
        path
        for path in sorted(experiments.glob("[0-9]" * 6))
        if load_experiment(path).state != "COMPLETE"
        or not (path / "published").is_file()
    ]
    if len(active) > 1:
        raise StateError("task contains more than one unfinished experiment")
    return active[0] if active else None


def _completed_experiment_count(task_directory: Path) -> int:
    experiments = task_directory / "experiments"
    if not experiments.is_dir():
        return 0
    return sum(
        1
        for path in experiments.glob("[0-9]" * 6)
        if load_experiment(path).state == "COMPLETE"
        and (path / "published").is_file()
    )


def _remove_experiment_worktrees(
    task: LocatedTask,
    experiment_id: int,
) -> None:
    for suffix in ("research", "champion", "candidate"):
        remove_worktree(
            task.config.repo,
            task.directory / "worktrees" / f"{experiment_id:06d}-{suffix}",
        )


def _discard_unreserved_experiment(
    task: LocatedTask,
    experiment: Path,
) -> None:
    record = load_experiment(experiment)
    if (experiment / "comparisons" / "primary" / "reservation.private.json").exists():
        raise StateError("reserved experiment cannot be discarded")
    _remove_experiment_worktrees(task, record.experiment_id)
    if record.candidate is not None:
        delete_ref(
            task.config.repo,
            f"refs/arctl/{task.config.task_id}/candidates/{record.experiment_id:06d}",
            record.candidate,
        )
    shutil.rmtree(experiment)


def _saved_evidence(
    experiment: Path,
    *,
    kind: str,
    trial_count: int,
    manifest: EvaluatorManifest,
) -> Evidence:
    path = experiment / "comparisons" / kind / "evidence.private.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"saved {kind} evidence is missing or invalid") from error
    if not isinstance(value, dict):
        raise StateError(f"saved {kind} evidence is invalid")
    try:
        return Evidence.from_mapping(
            value,
            expected_kind=kind,  # type: ignore[arg-type]
            expected_trial_count=trial_count,
            allowed_telemetry=manifest.public_telemetry,
            allowed_suspect_reasons=manifest.suspect_reason_codes,
        )
    except ValidationError as error:
        raise StateError(f"saved {kind} evidence is invalid") from error


def run_task(
    task: LocatedTask,
    *,
    max_experiments: int | None = None,
    research_command_builder: ResearchCommandBuilder = _default_research_command,
    public_check_command_builder: PublicCheckCommandBuilder | None = None,
    comparison_command_builder: CommandBuilder | None = None,
    calibration_command_builder: CalibrationCommandBuilder | None = None,
) -> RunOutcome:
    """Run a bounded sequence of fixed-trial experiments for one approved task."""
    approval = verify_approval(task.directory, task.config)
    manifest, manifest_hash = load_manifest(
        task.directory / "evaluator.manifest.json"
    )
    limit = task.config.max_experiments if max_experiments is None else max_experiments
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise StateError("max experiments must be a positive integer")
    remaining = task.config.max_experiments - _completed_experiment_count(
        task.directory
    )
    limit = min(limit, max(remaining, 0))

    initial_stop = task.directory / "stop.requested"
    if initial_stop.exists() and _active_experiment(task.directory) is None:
        initial_stop.unlink()
        return RunOutcome((), True)

    evaluator_worktree = task.directory / "worktrees" / "evaluator"
    _checkout(
        task.config.evaluator.repo,
        evaluator_worktree,
        approval["evaluator_commit"],
    )
    if task.config.trials == "auto":
        calibration_arguments: dict[str, Any] = {}
        if calibration_command_builder is not None:
            calibration_arguments["command_builder"] = calibration_command_builder
        trial_count = calibrate_trial_count(
            task.directory,
            task.config,
            manifest,
            manifest_hash=manifest_hash,
            evaluator_commit=approval["evaluator_commit"],
            evaluator_directory=evaluator_worktree,
            stop_path=task.directory / "stop.requested",
            **calibration_arguments,
        )
    else:
        trial_count = load_trial_count(task.directory, task.config)
    results: list[dict[str, Any]] = []
    stopped = False
    for _ in range(limit):
        stop = task.directory / "stop.requested"
        active = _active_experiment(task.directory)
        if stop.exists():
            if active is not None:
                record = load_experiment(active)
                primary_reservation = (
                    active
                    / "comparisons"
                    / "primary"
                    / "reservation.private.json"
                )
                if not primary_reservation.is_file():
                    _discard_unreserved_experiment(task, active)
                elif record.state in (
                    "PRIMARY_RESERVED",
                    "PROVISIONAL",
                    "SUSPECT_RESERVED",
                ):
                    request = load_research_request(
                        active / "request.public.json",
                        manifest=manifest,
                    )
                    saved_primary = (
                        active / "comparisons" / "primary" / "evidence.private.json"
                    )
                    primary = (
                        _saved_evidence(
                            active,
                            kind="primary",
                            trial_count=trial_count,
                            manifest=manifest,
                        )
                        if saved_primary.is_file()
                        else None
                    )
                    results.append(
                        publish_comparison_failure(
                            task.config,
                            active,
                            request,
                            manifest,
                            source="stop",
                            primary=primary,
                        )
                    )
                    _remove_experiment_worktrees(task, record.experiment_id)
                else:
                    # Valid final evidence is safer to publish than to replace.
                    stop.unlink()
                    stopped = True
                    # Continue below to recover FINALIZING/COMPLETE publication.
                if record.state not in ("FINALIZING", "COMPLETE"):
                    stop.unlink(missing_ok=True)
                    stopped = True
                    break
            else:
                stop.unlink()
                stopped = True
                break
        experiment = active
        if (
            experiment is not None
            and (experiment / "research.failure.json").is_file()
        ):
            _discard_unreserved_experiment(task, experiment)
            experiment = None
        if experiment is None:
            champion = resolve_commit(
                task.config.repo,
                f"refs/arctl/{task.config.task_id}/champion",
            )
            experiment, record = start_experiment(task.directory, champion)
        else:
            record = load_experiment(experiment)
        research_worktree = (
            task.directory / "worktrees" / f"{record.experiment_id:06d}-research"
        )
        candidate_worktree = (
            task.directory / "worktrees" / f"{record.experiment_id:06d}-candidate"
        )
        champion_worktree = (
            task.directory / "worktrees" / f"{record.experiment_id:06d}-champion"
        )
        try:
            if record.state == "RESEARCHING":
                _checkout(task.config.repo, research_worktree, record.champion)
                _run_research(
                    task,
                    experiment,
                    research_worktree,
                    manifest,
                    command_builder=research_command_builder,
                    stop_path=stop,
                )
                record, request = freeze_candidate(
                    experiment,
                    research_worktree,
                    task.config,
                    manifest,
                )
            else:
                request = load_research_request(
                    experiment / "request.public.json",
                    manifest=manifest,
                )

            if record.state == "CANDIDATE_FROZEN":
                if record.public_checks_passed is None:
                    check_arguments: dict[str, Any] = {}
                    if public_check_command_builder is not None:
                        check_arguments["command_builder"] = public_check_command_builder
                    check_arguments["stop_path"] = stop
                    run_public_checks(
                        task.directory,
                        experiment,
                        task.config,
                        **check_arguments,
                    )
                    record = load_experiment(experiment)
                if record.public_checks_passed is False:
                    from .experiment import publish_candidate_rejection

                    result = publish_candidate_rejection(task.config, experiment, request)
                    results.append(result)
                    _remove_experiment_worktrees(task, record.experiment_id)
                    continue
        except StoppedError:
            _discard_unreserved_experiment(task, experiment)
            stop.unlink(missing_ok=True)
            stopped = True
            break
        except ArctlError:
            if not (
                experiment / "comparisons" / "primary" / "reservation.private.json"
            ).exists() and not (experiment / "result.public.json").exists() and not (
                experiment / "research.failure.json"
            ).exists() and not (
                experiment / "public-check.failure.json"
            ).exists():
                _discard_unreserved_experiment(task, experiment)
            raise
        if record.candidate is None:
            raise StateError("active experiment has no frozen candidate")
        _checkout(task.config.repo, champion_worktree, record.champion)
        _checkout(task.config.repo, candidate_worktree, record.candidate)
        try:
            primary = _run_reserved_comparison(
                task,
                experiment,
                kind="primary",
                manifest=manifest,
                manifest_hash=manifest_hash,
                evaluator_commit=approval["evaluator_commit"],
                trial_count=trial_count,
                champion_worktree=champion_worktree,
                candidate_worktree=candidate_worktree,
                evaluator_worktree=evaluator_worktree,
                command_builder=comparison_command_builder,
                stop_path=stop,
            )
        except ComparisonFailure as error:
            current = load_experiment(experiment)
            if current.state != "PRIMARY_RESERVED":
                raise
            result = publish_comparison_failure(
                task.config,
                experiment,
                request,
                manifest,
                source=error.source,
            )
        else:
            current = load_experiment(experiment)
            if current.state == "PRIMARY_RESERVED":
                current = save_comparison_result(experiment, primary)
            state = current
            if state.state in ("PROVISIONAL", "SUSPECT_RESERVED"):
                try:
                    suspect = _run_reserved_comparison(
                        task,
                        experiment,
                        kind="suspect",
                        manifest=manifest,
                        manifest_hash=manifest_hash,
                        evaluator_commit=approval["evaluator_commit"],
                        trial_count=trial_count,
                        champion_worktree=champion_worktree,
                        candidate_worktree=candidate_worktree,
                        evaluator_worktree=evaluator_worktree,
                        command_builder=comparison_command_builder,
                        stop_path=stop,
                    )
                except ComparisonFailure as error:
                    result = publish_comparison_failure(
                        task.config,
                        experiment,
                        request,
                        manifest,
                        source=error.source,
                        primary=primary,
                    )
                else:
                    save_comparison_result(experiment, primary, suspect)
                    result = publish_final_result(
                        task.config,
                        experiment,
                        manifest,
                        request,
                        primary,
                        suspect,
                    )
            elif state.state in ("FINALIZING", "COMPLETE"):
                suspect_path = (
                    experiment / "comparisons" / "suspect" / "reservation.private.json"
                )
                suspect = (
                    _run_reserved_comparison(
                        task,
                        experiment,
                        kind="suspect",
                        manifest=manifest,
                        manifest_hash=manifest_hash,
                        evaluator_commit=approval["evaluator_commit"],
                        trial_count=trial_count,
                        champion_worktree=champion_worktree,
                        candidate_worktree=candidate_worktree,
                        evaluator_worktree=evaluator_worktree,
                        command_builder=comparison_command_builder,
                        stop_path=stop,
                    )
                    if suspect_path.is_file()
                    else None
                )
                result = publish_final_result(
                    task.config,
                    experiment,
                    manifest,
                    request,
                    primary,
                    suspect,
                )
            else:
                result = publish_final_result(
                    task.config,
                    experiment,
                    manifest,
                    request,
                    primary,
                )
        results.append(result)
        _remove_experiment_worktrees(task, record.experiment_id)
        if stop.exists():
            stop.unlink()
            stopped = True
            break
    return RunOutcome(tuple(results), stopped)
