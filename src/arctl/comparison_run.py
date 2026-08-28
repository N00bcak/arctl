"""The common prepare → subjects → score → validate comparison workflow."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from jsonschema import Draft202012Validator

from .commands import render_command
from .downstream import primary_process_error
from .comparison import ComparisonReservation
from .errors import ProcessError, StateError, StoppedError, ValidationError
from .git import ensure_clean_worktree, resolve_commit
from .manifest import EvaluatorManifest
from .models import Evidence
from .process import run_or_load_once
from .sandbox import (
    command_runtime_read_paths,
    marked_command,
    sandbox_command,
    sanitized_environment,
)
from .storage import atomic_write_json, write_json_once

FailureSource = Literal[
    "candidate",
    "champion",
    "evaluator",
    "evidence",
    "stop",
    "sandbox",
]
CommandBuilder = Callable[
    [
        Sequence[str],
        Path,
        Sequence[Path],
        Sequence[Path],
        str,
    ],
    Sequence[str],
]
ProgressCallback = Callable[[dict[str, Any]], None]
SUBJECT_WORKERS = 16


def _notify(
    progress: ProgressCallback | None,
    *,
    kind: str,
    stage: str,
    status: str,
    **fields: Any,
) -> None:
    if progress is not None:
        progress(
            {
                "event": "stage",
                "scope": "comparison",
                "kind": kind,
                "stage": stage,
                "status": status,
                **fields,
            }
        )


def _codex_command_builder(
    command: Sequence[str],
    cwd: Path,
    read_paths: Sequence[Path],
    write_paths: Sequence[Path],
    profile: str,
) -> Sequence[str]:
    return sandbox_command(
        command,
        cwd=cwd,
        read_paths=read_paths,
        write_paths=write_paths,
        profile=profile,
    )


class ComparisonFailure(StateError):
    """An official comparison failed in a controller-classified trust domain."""

    def __init__(self, source: FailureSource, message: str):
        super().__init__(message)
        self.source = source


def _load_json_object(
    path: Path, *, source: FailureSource, label: str
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ComparisonFailure(source, f"{label} was not written to the reserved path")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ComparisonFailure(source, f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ComparisonFailure(source, f"{label} must contain one JSON object")
    return value


def _run_process(
    directory: Path,
    command: Sequence[str],
    *,
    cwd: Path,
    manifest: EvaluatorManifest,
    source: FailureSource,
    codex_home: Path,
    writable_home: Path,
    stop_path: Path | None,
    execution_marker: Path,
) -> None:
    try:
        result = run_or_load_once(
            directory,
            command,
            timeout_seconds=manifest.limits.timeout_seconds,
            max_output_bytes=manifest.limits.max_output_bytes,
            cwd=cwd,
            env=sanitized_environment(
                codex_home=codex_home,
                writable_home=writable_home,
            ),
            stop_path=stop_path,
        )
    except StoppedError as error:
        raise ComparisonFailure("stop", str(error)) from error
    except (ProcessError, StateError) as error:
        raise ComparisonFailure(source, str(error)) from error
    if result["return_code"] != 0:
        if not execution_marker.is_file():
            raise ComparisonFailure(
                "sandbox",
                f"{source} sandbox did not start its reserved command",
            )
        detail = primary_process_error(directory)
        raise ComparisonFailure(
            source, f"{source} process exited unsuccessfully: {detail}"
        )


def _validate_batch(
    path: Path,
    *,
    trial_count: int,
    manifest: EvaluatorManifest,
) -> list[Any]:
    batch = _load_json_object(path, source="evaluator", label="public batch")
    if set(batch) != {"schema_version", "trial_count", "cases"}:
        raise ComparisonFailure("evaluator", "public batch fields are invalid")
    cases = batch["cases"]
    if (
        batch["schema_version"] != 1
        or batch["trial_count"] != trial_count
        or not isinstance(cases, list)
        or len(cases) != trial_count
    ):
        raise ComparisonFailure("evaluator", "public batch trial count is invalid")
    validator = Draft202012Validator(manifest.public_case_schema)
    for index, case in enumerate(cases):
        if not validator.is_valid(case):
            raise ComparisonFailure(
                "evaluator", f"public batch case {index} violates the approved schema"
            )
    return list(cases)


def _validate_subject_output(
    path: Path,
    *,
    subject: Literal["champion", "candidate"],
    trial_count: int,
    manifest: EvaluatorManifest,
) -> None:
    output = _load_json_object(path, source=subject, label=f"{subject} output")
    if set(output) != {"schema_version", "trial_count", "results"}:
        raise ComparisonFailure(subject, f"{subject} output fields are invalid")
    results = output["results"]
    if (
        output["schema_version"] != 1
        or output["trial_count"] != trial_count
        or not isinstance(results, list)
        or len(results) != trial_count
    ):
        raise ComparisonFailure(subject, f"{subject} result count is invalid")
    validator = Draft202012Validator(manifest.subject_result_schema)
    for index, result in enumerate(results):
        if not validator.is_valid(result):
            raise ComparisonFailure(
                subject, f"{subject} result {index} violates the approved schema"
            )


def _validate_prepare_response(
    path: Path,
    *,
    kind: str,
    trial_count: int,
) -> None:
    response = _load_json_object(path, source="evaluator", label="prepare response")
    expected = {
        "schema_version": 1,
        "operation": "prepare",
        "kind": kind,
        "trial_count": trial_count,
    }
    if response != expected:
        raise ComparisonFailure(
            "evaluator", "prepare response does not match its request"
        )


def _subject_case_shards(
    cases: Sequence[Any], *, subject_workers: int = SUBJECT_WORKERS
) -> tuple[tuple[Any, ...], ...]:
    """Partition ordered cases into balanced contiguous worker shards."""
    if (
        isinstance(subject_workers, bool)
        or not isinstance(subject_workers, int)
        or not 1 <= subject_workers <= SUBJECT_WORKERS
    ):
        raise ValueError(f"subject workers must be between 1 and {SUBJECT_WORKERS}")
    worker_count = min(subject_workers, len(cases))
    if worker_count == 0:
        return ()
    width, remainder = divmod(len(cases), worker_count)
    shards: list[tuple[Any, ...]] = []
    start = 0
    for index in range(worker_count):
        end = start + width + (1 if index < remainder else 0)
        shards.append(tuple(cases[start:end]))
        start = end
    return tuple(shards)


def _run_subject_arm(
    directory: Path,
    *,
    process_root: Path,
    outputs: Path,
    cases: Sequence[Any],
    subject: Literal["champion", "candidate"],
    subject_directory: Path,
    manifest: EvaluatorManifest,
    command_builder: CommandBuilder,
    codex_home: Path,
    stop_path: Path | None,
    subject_workers: int = SUBJECT_WORKERS,
) -> tuple[Path, bool]:
    """Run one arm in isolated case shards and assemble its canonical ordered output."""
    output_directory = outputs / subject
    output_directory.mkdir(parents=True, exist_ok=True)
    output = output_directory / "result.json"
    if output.is_file():
        _validate_subject_output(
            output,
            subject=subject,
            trial_count=len(cases),
            manifest=manifest,
        )
        return output, True
    if (process_root / subject / "started.json").is_file():
        raise ComparisonFailure(
            subject,
            f"legacy serial {subject} process started without a recoverable output",
        )

    shards = _subject_case_shards(cases, subject_workers=subject_workers)

    def run_worker(index: int, shard: tuple[Any, ...]) -> None:
        worker = f"{index:04d}"
        worker_output = output_directory / "workers" / worker
        worker_output.mkdir(parents=True, exist_ok=True)
        worker_batch = worker_output / "batch.public.json"
        worker_result = worker_output / "result.json"
        write_json_once(
            worker_batch,
            {
                "schema_version": 1,
                "trial_count": len(shard),
                "cases": list(shard),
            },
        )
        command = render_command(
            manifest.subject_command,
            {"input": worker_batch, "output": worker_result},
            allowed_roots=(directory,),
        )
        _run_process(
            process_root / subject / worker,
            command_builder(
                marked_command(command, worker_output / "execution.started"),
                subject_directory,
                (
                    subject_directory,
                    worker_batch,
                    *command_runtime_read_paths(command),
                ),
                (worker_output,),
                "arctl-subject",
            ),
            cwd=subject_directory,
            manifest=manifest,
            source=subject,
            codex_home=codex_home,
            writable_home=worker_output,
            stop_path=stop_path,
            execution_marker=worker_output / "execution.started",
        )
        _validate_subject_output(
            worker_result,
            subject=subject,
            trial_count=len(shard),
            manifest=manifest,
        )

    with ThreadPoolExecutor(max_workers=len(shards)) as executor:
        futures = [
            executor.submit(run_worker, index, shard)
            for index, shard in enumerate(shards, start=1)
        ]
        failures: list[Exception] = []
        for future in futures:
            try:
                future.result()
            except Exception as error:
                failures.append(error)
        if failures:
            raise failures[0]

    combined: list[Any] = []
    for index, shard in enumerate(shards, start=1):
        worker_result = output_directory / "workers" / f"{index:04d}" / "result.json"
        value = _load_json_object(
            worker_result,
            source=subject,
            label=f"{subject} worker {index} output",
        )
        combined.extend(value["results"])
    write_json_once(
        output,
        {
            "schema_version": 1,
            "trial_count": len(cases),
            "results": combined,
        },
    )
    _validate_subject_output(
        output,
        subject=subject,
        trial_count=len(cases),
        manifest=manifest,
    )
    return output, False


def run_comparison(
    directory: Path,
    reservation: ComparisonReservation,
    manifest: EvaluatorManifest,
    *,
    manifest_hash: str,
    evaluator_directory: Path,
    champion_directory: Path,
    candidate_directory: Path,
    command_builder: CommandBuilder = _codex_command_builder,
    stop_path: Path | None = None,
    progress: ProgressCallback | None = None,
    subject_workers: int = SUBJECT_WORKERS,
) -> Evidence:
    """Run or recover one immutable comparison without redrawing any process."""
    expected_commands = {
        "subject": manifest.subject_command,
        "prepare": manifest.prepare_command,
        "score": manifest.score_command,
    }
    if dict(reservation.commands) != expected_commands:
        raise ComparisonFailure(
            "evidence", "reserved commands differ from the manifest"
        )
    if manifest_hash != reservation.manifest:
        raise ComparisonFailure(
            "evidence", "manifest hash differs from the reservation"
        )
    for source, checkout, revision in (
        ("evaluator", evaluator_directory, reservation.evaluator),
        ("champion", champion_directory, reservation.champion),
        ("candidate", candidate_directory, reservation.candidate),
    ):
        try:
            actual = resolve_commit(checkout, "HEAD")
            ensure_clean_worktree(checkout)
        except StateError as error:
            raise ComparisonFailure(
                source,
                f"{source} checkout is not a clean Git revision",
            ) from error
        if actual != revision:
            raise ComparisonFailure(
                source, f"{source} checkout differs from the reservation"
            )

    evidence_path = directory / "evidence.private.json"
    if evidence_path.exists():
        _notify(
            progress,
            kind=reservation.kind,
            stage="comparison",
            status="recovered",
            trial_count=reservation.trial_count,
        )
        raw = _load_json_object(
            evidence_path, source="evidence", label="saved evidence"
        )
        try:
            return Evidence.from_mapping(
                raw,
                expected_kind=reservation.kind,
                expected_trial_count=reservation.trial_count,
                allowed_telemetry=manifest.public_telemetry,
                allowed_suspect_reasons=manifest.suspect_reason_codes,
            )
        except ValidationError as error:
            raise ComparisonFailure("evidence", "saved evidence is invalid") from error

    directory.mkdir(parents=True, exist_ok=True)
    process_root = directory / "process"
    codex_home = directory / "sandbox-home"
    codex_home.mkdir(exist_ok=True)
    requests = directory / "requests"
    outputs = directory / "outputs"
    prepare_output = outputs / "prepare"
    prepare_output.mkdir(parents=True, exist_ok=True)
    batch_path = prepare_output / "batch.public.json"
    scoring_path = prepare_output / "scoring.private.json"
    prepare_request = requests / "prepare.json"
    prepare_response = prepare_output / "response.json"
    write_json_once(
        prepare_request,
        {
            "schema_version": 1,
            "operation": "prepare",
            "kind": reservation.kind,
            "experiment_id": reservation.experiment_id,
            "trial_count": reservation.trial_count,
            "trial_seeds": list(reservation.trial_seeds),
            "public_batch": str(batch_path.resolve()),
            "private_scoring": str(scoring_path.resolve()),
        },
    )
    prepare_command = render_command(
        manifest.prepare_command,
        {"request": prepare_request, "response": prepare_response},
        allowed_roots=(directory,),
    )
    prepare_process = process_root / "prepare"
    prepare_recovered = (prepare_process / "result.json").is_file()
    _notify(
        progress,
        kind=reservation.kind,
        stage="prepare",
        status="started",
        trial_count=reservation.trial_count,
    )
    _run_process(
        prepare_process,
        command_builder(
            marked_command(prepare_command, prepare_output / "execution.started"),
            evaluator_directory,
            (
                evaluator_directory,
                prepare_request,
                *command_runtime_read_paths(prepare_command),
            ),
            (prepare_output,),
            "arctl-evaluator",
        ),
        cwd=evaluator_directory,
        manifest=manifest,
        source="evaluator",
        codex_home=codex_home,
        writable_home=prepare_output,
        stop_path=stop_path,
        execution_marker=prepare_output / "execution.started",
    )
    _notify(
        progress,
        kind=reservation.kind,
        stage="prepare",
        status="recovered" if prepare_recovered else "complete",
        trial_count=reservation.trial_count,
    )
    _validate_prepare_response(
        prepare_response,
        kind=reservation.kind,
        trial_count=reservation.trial_count,
    )
    cases = _validate_batch(
        batch_path,
        trial_count=reservation.trial_count,
        manifest=manifest,
    )
    if scoring_path.is_symlink() or not scoring_path.is_file():
        raise ComparisonFailure(
            "evaluator", "private scoring data was not written to the reserved path"
        )

    subject_outputs: dict[str, Path] = {}
    subject_directories: Mapping[str, Path] = {
        "champion": champion_directory,
        "candidate": candidate_directory,
    }
    for subject_index, subject in enumerate(reservation.subject_order, start=1):
        _notify(
            progress,
            kind=reservation.kind,
            stage="subject",
            status="started",
            batch=subject_index,
            batches=2,
            trial_count=reservation.trial_count,
            workers=min(subject_workers, reservation.trial_count),
        )
        output, subject_recovered = _run_subject_arm(
            directory,
            process_root=process_root,
            outputs=outputs,
            cases=cases,
            subject=subject,  # type: ignore[arg-type]
            subject_directory=subject_directories[subject],
            manifest=manifest,
            command_builder=command_builder,
            codex_home=codex_home,
            stop_path=stop_path,
            subject_workers=subject_workers,
        )
        _notify(
            progress,
            kind=reservation.kind,
            stage="subject",
            status="recovered" if subject_recovered else "complete",
            batch=subject_index,
            batches=2,
            trial_count=reservation.trial_count,
            workers=min(subject_workers, reservation.trial_count),
        )
        subject_outputs[subject] = output

    score_output = outputs / "score"
    score_output.mkdir(parents=True, exist_ok=True)
    score_request = requests / "score.json"
    score_response = score_output / "evidence.json"
    write_json_once(
        score_request,
        {
            "schema_version": 1,
            "operation": "score",
            "kind": reservation.kind,
            "experiment_id": reservation.experiment_id,
            "trial_count": reservation.trial_count,
            "private_scoring": str(scoring_path.resolve()),
            "champion_output": str(subject_outputs["champion"].resolve()),
            "candidate_output": str(subject_outputs["candidate"].resolve()),
        },
    )
    score_command = render_command(
        manifest.score_command,
        {"request": score_request, "response": score_response},
        allowed_roots=(directory,),
    )
    score_process = process_root / "score"
    score_recovered = (score_process / "result.json").is_file()
    _notify(
        progress,
        kind=reservation.kind,
        stage="score",
        status="started",
        trial_count=reservation.trial_count,
    )
    _run_process(
        score_process,
        command_builder(
            marked_command(score_command, score_output / "execution.started"),
            evaluator_directory,
            (
                evaluator_directory,
                score_request,
                scoring_path,
                subject_outputs["champion"],
                subject_outputs["candidate"],
                *command_runtime_read_paths(score_command),
            ),
            (score_output,),
            "arctl-evaluator",
        ),
        cwd=evaluator_directory,
        manifest=manifest,
        source="evaluator",
        codex_home=codex_home,
        writable_home=score_output,
        stop_path=stop_path,
        execution_marker=score_output / "execution.started",
    )
    _notify(
        progress,
        kind=reservation.kind,
        stage="score",
        status="recovered" if score_recovered else "complete",
        trial_count=reservation.trial_count,
    )
    _notify(
        progress,
        kind=reservation.kind,
        stage="validate",
        status="started",
        trial_count=reservation.trial_count,
    )
    raw_evidence = _load_json_object(
        score_response,
        source="evidence",
        label="comparison evidence",
    )
    try:
        evidence = Evidence.from_mapping(
            raw_evidence,
            expected_kind=reservation.kind,
            expected_trial_count=reservation.trial_count,
            allowed_telemetry=manifest.public_telemetry,
            allowed_suspect_reasons=manifest.suspect_reason_codes,
        )
    except ValidationError as error:
        raise ComparisonFailure("evidence", "comparison evidence is invalid") from error
    atomic_write_json(evidence_path, raw_evidence)
    _notify(
        progress,
        kind=reservation.kind,
        stage="validate",
        status="complete",
        trial_count=reservation.trial_count,
    )
    return evidence
