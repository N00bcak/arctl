"""Persistent single-task experiment orchestration."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .approval import verify_approval
from .calibration import CalibrationCommandBuilder, calibrate_trial_count
from .candidate_review import AgentCommandBuilder as ReviewCommandBuilder
from .candidate_review import review_candidate
from .comparison import ComparisonReservation, load_reservation, reserve_comparison
from .comparison_run import CommandBuilder, ComparisonFailure, run_comparison
from .downstream import transient_process_error
from .errors import (
    ArctlError,
    ProcessError,
    ResearchMiss,
    StateError,
    StoppedError,
    TransientDownstreamError,
    ValidationError,
)
from .experiment import (
    complete_reflection,
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
    candidate_changed_paths,
    create_candidate_commit,
    create_detached_worktree,
    delete_ref,
    ensure_clean_worktree,
    remove_worktree,
    resolve_commit,
)
from .manifest import EvaluatorManifest
from .models import Evidence, ResearchRequest
from .process import run_or_load_once
from .reflection import ReflectionCommandBuilder, run_reflection, validate_reflection
from .registry import LocatedTask
from .results import validate_public_result
from .sandbox import command_runtime_read_paths, research_command, sanitized_environment
from .storage import atomic_write_text, write_json_once
from .search import (
    AgentCommandBuilder,
    add_ledger_entry,
    ensure_strategy,
    load_ledger,
    next_search_id,
    record_miss,
    research_schema,
    validate_research_links,
)
from .taskio import load_manifest
from .trials import load_trial_count, load_trial_count_record

ResearchCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]
PublicCheckCommandBuilder = Callable[[Sequence[str], Path, Path], Sequence[str]]
ProgressCallback = Callable[[dict[str, Any]], None]
_DEFAULT_STRATEGY = object()


def _notify(
    progress: ProgressCallback | None,
    event: str,
    **fields: Any,
) -> None:
    if progress is not None:
        progress({"event": event, **fields})

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
    schema = research_schema(manifest)
    _validate_codex_output_schema(schema)
    return schema


@dataclass(frozen=True)
class RunOutcome:
    results: tuple[dict[str, Any], ...]
    stopped: bool
    stalled: bool = False
    reflection_failed: bool = False
    limit_reached: bool = False


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


def _compatibility_strategy_command(
    _worktree: Path,
    scratch: Path,
    _schema: Path,
    prompt: str,
) -> Sequence[str]:
    """Supply a neutral strategy for legacy injected research backends."""
    packet = json.loads(prompt.split("\n\n", 1)[1])
    source_id = next(
        source["id"]
        for source in packet["environment_sources"]
        if source["kind"] != "probe"
    )
    value = {
        "schema_version": 2,
        "environment_observations": [
            {
                "id": "environment-baseline",
                "claim": "The injected backend owns environment analysis.",
                "status": "inferred",
                "evidence": [
                    {
                        "source_id": source_id,
                        "location": "injected backend",
                        "finding": "A public environment is available for inspection.",
                    }
                ],
            }
        ],
        "environment_uncertainties": [],
        "successful_policy_behaviors": [
            {
                "id": "environment-compatible",
                "behavior": "Respond effectively to the public environment.",
                "derived_from": ["environment-baseline"],
                "rationale": "The policy must act through the declared environment.",
                "tradeoffs": [],
            }
        ],
    }
    script = (
        "import json,pathlib,sys;"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(json.loads(sys.argv[2])))"
    )
    return (
        "python3",
        "-c",
        script,
        str(scratch / "strategy.public.json"),
        json.dumps(value),
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
    strategy_files = sorted((task.directory / "strategy").glob("*.public.json"))
    strategy = (
        json.loads(strategy_files[-1].read_text(encoding="utf-8"))
        if strategy_files
        else None
    )
    packet = {
        "objective": task.config.objective,
        "editable_paths": list(task.config.editable_paths),
        "denied_paths": list(task.config.denied_paths),
        "public_checks": [list(command) for command in task.config.public_checks],
        "public_probe": list(task.config.public_probe),
        "statistic": manifest.public_statistic,
        "subject_interface": manifest.subject_interface,
        "telemetry": {
            name: asdict(metric)
            for name, metric in manifest.public_telemetry.items()
        },
        "completed_results": _public_history(task, manifest),
        "strategy": strategy,
        "exploration_ledger": str(task.directory / "exploration" / "ledger.public.jsonl"),
        "candidate_review_contract": (
            task.config.candidate_review.contract
            if task.config.candidate_review is not None
            else None
        ),
    }
    return (
        "Make one focused improvement to the checked-out champion for this public "
        "research task. First select one successful-policy behavior from the strategy "
        "and name its id as strategy_behavior_id. "
        "Then propose a concrete policy mechanism, reason about its viability, and scan "
        "the exploration ledger's prior results, telemetry, and reflections for evidence "
        "that supports, contradicts, or leaves it unresolved. State whether the work is "
        "new or refines a named ledger entry. Prefer a material mechanism over an "
        "unsupported one-off constant tweak when the editable implementation permits "
        "structural work; substantiate numeric-only changes with a deliberate public "
        "sweep. Treat candidate_review_contract as a hard pre-trial constraint when it "
        "is present. Stay within editable_paths and do not "
        "commit. Run public checks or declared probes when useful. Your final response "
        "must be only "
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
    prompt = _research_prompt(task, manifest)
    if command_builder is _default_research_command:
        runtime_paths: list[Path] = []
        for public_command in (*task.config.public_checks, task.config.public_probe):
            runtime_paths.extend(command_runtime_read_paths(public_command))
        ledger = task.directory / "exploration" / "ledger.public.jsonl"
        if ledger.is_file():
            runtime_paths.append(ledger)
        command = research_command(
            worktree=worktree,
            scratch=scratch,
            output_schema=schema,
            prompt=prompt,
            read_paths=tuple(
                dict.fromkeys(
                    path
                    for path in runtime_paths
                    if not path.is_relative_to(worktree)
                )
            ),
            model=task.config.execution_model,
            reasoning_effort=task.config.execution_reasoning_effort,
        )
        codex_home = Path(
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        )
        environment = sanitized_environment(
            codex_home=codex_home,
            writable_home=scratch,
        )
    else:
        command = command_builder(worktree, scratch, schema, prompt)
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
        except ProcessError as error:
            transient = transient_process_error(
                experiment / "process" / "research",
                stage="execution",
                codex=command_builder is _default_research_command,
                fallback=str(error),
            )
            if transient is not None:
                raise transient from error
            raise StateError("fresh research session did not complete") from error
        except StateError as error:
            raise StateError("fresh research session did not complete") from error
        if result["return_code"] != 0:
            transient = transient_process_error(
                experiment / "process" / "research",
                stage="execution",
                codex=command_builder is _default_research_command,
            )
            if transient is not None:
                raise transient
            raise StateError("fresh research session exited unsuccessfully")
        request_path = scratch / "request.public.json"
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResearchMiss(
                "invalid_request",
                "fresh research session did not write valid request JSON"
            ) from error
        if not isinstance(request, dict):
            raise ResearchMiss(
                "invalid_request",
                "fresh research request must contain one JSON object",
            )
        telemetry = request.get("expected_telemetry")
        if isinstance(telemetry, dict):
            request["expected_telemetry"] = {
                name: expectation
                for name, expectation in telemetry.items()
                if expectation is not None
            }
    except (StateError, TransientDownstreamError) as error:
        write_json_once(
            experiment / "research.failure.json",
            {
                "schema_version": 1,
                "message": str(error),
            },
        )
        raise
    write_json_once(experiment / "request.public.json", request)


def _candidate_search(
    task: LocatedTask,
    manifest: EvaluatorManifest,
    *,
    champion: str,
    research_command_builder: ResearchCommandBuilder,
    strategy_command_builder: AgentCommandBuilder | None,
    review_command_builder: ReviewCommandBuilder | None,
    repair_command_builder: ReviewCommandBuilder | None,
    check_command_builder: PublicCheckCommandBuilder | None,
    stop_path: Path,
    progress: ProgressCallback | None,
) -> tuple[Path, str, dict[str, Any], Path] | None:
    """Return one novel controller-created candidate, or a bounded stall."""
    recovered = _recover_candidate_review(
        task,
        manifest,
        champion=champion,
        review_command_builder=review_command_builder,
        repair_command_builder=repair_command_builder,
        check_command_builder=check_command_builder,
        stop_path=stop_path,
        progress=progress,
    )
    if recovered is not None:
        return recovered
    search_id = next_search_id(task.directory)
    search = task.directory / "searches" / f"{search_id:06d}"
    strategy_worktree = task.directory / "worktrees" / f"search-{search_id:06d}-strategy"
    _checkout(task.config.repo, strategy_worktree, champion)
    try:
        revision, _ = ensure_strategy(
            task,
            strategy_worktree,
            manifest,
            refresh=False,
            command_builder=strategy_command_builder,
            stop_path=stop_path,
        )
    finally:
        remove_worktree(task.config.repo, strategy_worktree)
    _notify(progress, "strategy", revision=revision, refresh=False)

    for attempt in range(1, 7):
        if attempt == 4:
            _checkout(task.config.repo, strategy_worktree, champion)
            try:
                revision, _ = ensure_strategy(
                    task,
                    strategy_worktree,
                    manifest,
                    refresh=True,
                    command_builder=strategy_command_builder,
                    stop_path=stop_path,
                )
            finally:
                remove_worktree(task.config.repo, strategy_worktree)
            _notify(progress, "strategy", revision=revision, refresh=True)
        attempt_directory = search / "attempts" / f"{attempt:02d}"
        worktree = task.directory / "worktrees" / f"search-{search_id:06d}-attempt-{attempt:02d}"
        _notify(progress, "search_attempt", search_id=search_id, attempt=attempt, attempts=6)
        _checkout(task.config.repo, worktree, champion)
        try:
            _run_research(
                task,
                attempt_directory,
                worktree,
                manifest,
                command_builder=research_command_builder,
                stop_path=stop_path,
            )
            request_path = attempt_directory / "request.public.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise ResearchMiss("invalid_request", "research request is not an object")
            try:
                ResearchRequest.from_mapping(
                    request,
                    allowed_telemetry=manifest.public_telemetry,
                )
                strategy_files = sorted(
                    (task.directory / "strategy").glob("*.public.json")
                )
                strategy = json.loads(strategy_files[-1].read_text(encoding="utf-8"))
                validate_research_links(
                    request,
                    strategy=strategy,
                    ledger=load_ledger(task.directory),
                )
            except ValidationError as error:
                raise ResearchMiss("invalid_request", str(error)) from error
            review_candidate(
                task,
                manifest,
                worktree=worktree,
                attempt_directory=attempt_directory,
                champion=champion,
                request=request,
                stop_path=stop_path,
                review_command_builder=review_command_builder,
                repair_command_builder=repair_command_builder,
                check_command_builder=check_command_builder,
                progress=progress,
            )
            candidate, changed_paths = create_candidate_commit(
                worktree,
                champion=champion,
                editable_paths=task.config.editable_paths,
                denied_paths=task.config.denied_paths,
                prior_candidate_ref_prefix=f"refs/arctl/{task.config.task_id}/candidates/",
                message=f"arctl search {search_id} attempt {attempt}",
            )
        except StoppedError:
            remove_worktree(task.config.repo, worktree)
            raise
        except TransientDownstreamError as error:
            if error.stage == "execution":
                remove_worktree(task.config.repo, worktree)
            raise
        except ResearchMiss as miss:
            try:
                raw = json.loads((attempt_directory / "request.public.json").read_text(encoding="utf-8"))
                saved_request = raw if isinstance(raw, dict) else None
            except (OSError, json.JSONDecodeError):
                saved_request = None
            record_miss(
                task,
                search_id=search_id,
                attempt=attempt,
                champion=champion,
                request=saved_request,
                miss=miss,
            )
            _notify(progress, "search_miss", attempt=attempt, code=miss.code, message=str(miss))
            remove_worktree(task.config.repo, worktree)
            continue
        write_json_once(
            attempt_directory / "candidate.public.json",
            {
                "schema_version": 1,
                "candidate": candidate,
                "changed_paths": list(changed_paths),
            },
        )
        return worktree, candidate, request, attempt_directory
    write_json_once(
        search / "stalled.public.json",
        {"schema_version": 1, "search_id": search_id, "attempts": 6},
    )
    return None


def _recover_candidate_review(
    task: LocatedTask,
    manifest: EvaluatorManifest,
    *,
    champion: str,
    review_command_builder: ReviewCommandBuilder | None,
    repair_command_builder: ReviewCommandBuilder | None,
    check_command_builder: PublicCheckCommandBuilder | None,
    stop_path: Path,
    progress: ProgressCallback | None,
) -> tuple[Path, str, dict[str, Any], Path] | None:
    """Resume a candidate whose review stopped for operational reasons."""
    if task.config.candidate_review is None:
        return None
    searches = sorted((task.directory / "searches").glob("[0-9]" * 6), reverse=True)
    for search in searches:
        if (search / "stalled.public.json").is_file():
            continue
        search_id = int(search.name)
        attempts = sorted((search / "attempts").glob("[0-9][0-9]"), reverse=True)
        for attempt_directory in attempts:
            if not (attempt_directory / "candidate-review").is_dir():
                continue
            attempt = int(attempt_directory.name)
            worktree = (
                task.directory
                / "worktrees"
                / f"search-{search_id:06d}-attempt-{attempt:02d}"
            )
            if not worktree.is_dir():
                continue
            try:
                request = json.loads(
                    (attempt_directory / "request.public.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, json.JSONDecodeError) as error:
                raise StateError("recoverable candidate review has invalid request") from error
            if not isinstance(request, dict):
                raise StateError("recoverable candidate review has invalid request")
            ResearchRequest.from_mapping(
                request,
                allowed_telemetry=manifest.public_telemetry,
            )
            strategy_files = sorted(
                (task.directory / "strategy").glob("*.public.json")
            )
            if not strategy_files:
                raise StateError("recoverable candidate review has no strategy")
            strategy = json.loads(strategy_files[-1].read_text(encoding="utf-8"))
            validate_research_links(
                request,
                strategy=strategy,
                ledger=load_ledger(task.directory),
            )
            _notify(
                progress,
                "search_attempt",
                search_id=search_id,
                attempt=attempt,
                attempts=6,
            )
            review_candidate(
                task,
                manifest,
                worktree=worktree,
                attempt_directory=attempt_directory,
                champion=champion,
                request=request,
                stop_path=stop_path,
                review_command_builder=review_command_builder,
                repair_command_builder=repair_command_builder,
                check_command_builder=check_command_builder,
                progress=progress,
            )
            candidate_path = attempt_directory / "candidate.public.json"
            if candidate_path.is_file():
                saved = json.loads(candidate_path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict) or not isinstance(
                    saved.get("candidate"), str
                ):
                    raise StateError("recoverable candidate record is invalid")
                return worktree, saved["candidate"], request, attempt_directory
            candidate, changed_paths = create_candidate_commit(
                worktree,
                champion=champion,
                editable_paths=task.config.editable_paths,
                denied_paths=task.config.denied_paths,
                prior_candidate_ref_prefix=(
                    f"refs/arctl/{task.config.task_id}/candidates/"
                ),
                message=f"arctl search {search_id} attempt {attempt}",
            )
            write_json_once(
                candidate_path,
                {
                    "schema_version": 1,
                    "candidate": candidate,
                    "changed_paths": list(changed_paths),
                },
            )
            return worktree, candidate, request, attempt_directory
    return None


def _record_official_result(
    task: LocatedTask,
    result: Mapping[str, Any],
    manifest: EvaluatorManifest,
) -> None:
    request_path = (
        task.directory
        / "experiments"
        / f"{int(result['experiment_id']):06d}"
        / "request.public.json"
    )
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"published research request is invalid: {request_path}") from error
    if not isinstance(request, dict):
        raise StateError(f"published research request is invalid: {request_path}")
    entry = {
            "schema_version": 1,
            "source": f"experiment:{int(result['experiment_id']):06d}",
            "kind": "experiment",
            "champion": result["champion_before"],
            "candidate": result["candidate"],
            "claim": result["hypothesis"],
            "strategy_behavior_id": request.get("strategy_behavior_id"),
            "mechanism": request.get("mechanism"),
            "viability": request.get("viability"),
            "evidence_review": request.get("evidence_review"),
            "lineage": request.get("lineage"),
            "decision": result["decision"],
            "changed_paths": list(
                candidate_changed_paths(
                    task.config.repo, result["champion_before"], result["candidate"]
                )
            ),
        }
    reflection_path = request_path.parent / "reflection.public.json"
    if reflection_path.is_file():
        try:
            reflection = json.loads(reflection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("published reflection is invalid") from error
        reflection = validate_reflection(
            reflection, metric_names=tuple(manifest.public_telemetry)
        )
        entry["reflection"] = {
            "status": reflection.get("status"),
            "warning": reflection.get("warning"),
            "assessment": reflection.get("assessment"),
        }
    add_ledger_entry(task.directory, entry)


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
    progress: ProgressCallback | None,
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
        progress=progress,
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


def _reflect_final_result(
    task: LocatedTask,
    experiment: Path,
    manifest: EvaluatorManifest,
    result: dict[str, Any],
    *,
    command_builder: ReflectionCommandBuilder | None,
    stop_path: Path,
    progress: ProgressCallback | None,
) -> None:
    record = load_experiment(experiment)
    if record.state != "REFLECTING" or record.candidate is None:
        raise StateError("post-trial reflection requires a final result")
    try:
        request = json.loads(
            (experiment / "request.public.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("public research request is invalid") from error
    if not isinstance(request, dict):
        raise StateError("public research request is invalid")
    champion_worktree = (
        task.directory / "worktrees" / f"{record.experiment_id:06d}-champion"
    )
    candidate_worktree = (
        task.directory / "worktrees" / f"{record.experiment_id:06d}-candidate"
    )
    _checkout(task.config.repo, champion_worktree, record.champion)
    _checkout(task.config.repo, candidate_worktree, record.candidate)
    _notify(progress, "reflection", experiment_id=record.experiment_id)
    run_reflection(
        task=task.config,
        experiment=experiment,
        manifest=manifest,
        request=request,
        result=result,
        candidate_worktree=candidate_worktree,
        champion_worktree=champion_worktree,
        stop_path=stop_path,
        command_builder=command_builder,
    )
    complete_reflection(task.config, experiment, result)
    _notify(progress, "reflection_complete", experiment_id=record.experiment_id)


def _mark_reflection_failed(experiment: Path) -> None:
    write_json_once(
        experiment / "reflection.blocked.json",
        {
            "schema_version": 1,
            "message": "Post-trial reflection did not complete; later research is blocked.",
        },
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
    strategy_command_builder: AgentCommandBuilder | None | object = _DEFAULT_STRATEGY,
    review_command_builder: ReviewCommandBuilder | None = None,
    repair_command_builder: ReviewCommandBuilder | None = None,
    public_check_command_builder: PublicCheckCommandBuilder | None = None,
    comparison_command_builder: CommandBuilder | None = None,
    calibration_command_builder: CalibrationCommandBuilder | None = None,
    reflection_command_builder: ReflectionCommandBuilder | None = None,
    progress: ProgressCallback | None = None,
) -> RunOutcome:
    """Run a bounded sequence of fixed-trial experiments for one approved task."""
    if strategy_command_builder is _DEFAULT_STRATEGY:
        strategy_command_builder = (
            None
            if research_command_builder is _default_research_command
            else _compatibility_strategy_command
        )
    approval = verify_approval(task.directory, task.config)
    manifest, manifest_hash = load_manifest(
        task.directory / "evaluator.manifest.json"
    )
    for published in _public_history(task, manifest):
        _record_official_result(task, published, manifest)
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
        _notify(progress, "calibration")
        calibration_arguments: dict[str, Any] = {}
        if calibration_command_builder is not None:
            calibration_arguments["command_builder"] = calibration_command_builder
        calibration_champion = (
            task.directory / "worktrees" / "calibration-champion"
        )
        if manifest.calibration.controller_pilot:
            champion = approval["approved_champion"]
            if (
                calibration_champion.exists()
                and resolve_commit(calibration_champion, "HEAD")
                != resolve_commit(task.config.repo, champion)
            ):
                ensure_clean_worktree(calibration_champion)
                remove_worktree(task.config.repo, calibration_champion)
            _checkout(task.config.repo, calibration_champion, champion)
            calibration_arguments["champion_directory"] = calibration_champion
        trial_count = calibrate_trial_count(
            task.directory,
            task.config,
            manifest,
            manifest_hash=manifest_hash,
            evaluator_commit=approval["evaluator_commit"],
            approved_champion=approval["approved_champion"],
            evaluator_directory=evaluator_worktree,
            stop_path=task.directory / "stop.requested",
            progress=progress,
            **calibration_arguments,
        )
        if manifest.calibration.controller_pilot:
            remove_worktree(task.config.repo, calibration_champion)
    else:
        trial_count = load_trial_count(task.directory, task.config)
    trial_record = load_trial_count_record(task.directory, task.config)
    _notify(
        progress,
        "ready",
        trial_count=trial_count,
        calibration_summary=trial_record.get("calibration"),
    )
    results: list[dict[str, Any]] = []
    stopped = False
    stalled = False
    reflection_failed = False
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
        if experiment is not None and load_experiment(experiment).state == "REFLECTING":
            try:
                raw_result = json.loads(
                    (experiment / "result.public.json").read_text(encoding="utf-8")
                )
                if not isinstance(raw_result, dict):
                    raise StateError("saved final result is invalid")
                result_value = validate_public_result(
                    raw_result,
                    allowed_telemetry=manifest.public_telemetry,
                    allowed_suspect_reasons=manifest.suspect_reason_codes,
                    expected_statistic=manifest.public_statistic,
                )
                _reflect_final_result(
                    task,
                    experiment,
                    manifest,
                    result_value,
                    command_builder=reflection_command_builder,
                    stop_path=stop,
                    progress=progress,
                )
            except StoppedError:
                stop.unlink(missing_ok=True)
                stopped = True
                break
            except (OSError, json.JSONDecodeError, StateError):
                _mark_reflection_failed(experiment)
                reflection_failed = True
                _notify(
                    progress,
                    "reflection_failed",
                    experiment_id=load_experiment(experiment).experiment_id,
                )
                break
            results.append(result_value)
            _record_official_result(task, result_value, manifest)
            _notify(progress, "result", result=result_value)
            _remove_experiment_worktrees(
                task, load_experiment(experiment).experiment_id
            )
            continue
        if (
            experiment is not None
            and (experiment / "research.failure.json").is_file()
        ):
            _discard_unreserved_experiment(task, experiment)
            experiment = None
        presearched = False
        if experiment is None:
            champion = resolve_commit(
                task.config.repo,
                f"refs/arctl/{task.config.task_id}/champion",
            )
            try:
                found = _candidate_search(
                    task,
                    manifest,
                    champion=champion,
                    research_command_builder=research_command_builder,
                    strategy_command_builder=strategy_command_builder,  # type: ignore[arg-type]
                    review_command_builder=review_command_builder,
                    repair_command_builder=repair_command_builder,
                    check_command_builder=public_check_command_builder,
                    stop_path=stop,
                    progress=progress,
                )
            except StoppedError:
                stop.unlink(missing_ok=True)
                stopped = True
                break
            if found is None:
                stalled = True
                break
            research_worktree, candidate, raw_request, search_attempt = found
            experiment, record = start_experiment(task.directory, champion)
            write_json_once(experiment / "request.public.json", raw_request)
            atomic_write_text(experiment / "candidate.commit", candidate + "\n")
            review_directory = search_attempt / "candidate-review"
            if review_directory.is_dir():
                shutil.copytree(review_directory, experiment / "candidate-review")
            presearched = True
        else:
            record = load_experiment(experiment)
        _notify(
            progress,
            "experiment",
            experiment_id=record.experiment_id,
            limit=limit,
        )
        if not presearched:
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
                if not presearched:
                    _notify(progress, "research", experiment_id=record.experiment_id)
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
                if presearched:
                    remove_worktree(task.config.repo, research_worktree)
                _notify(
                    progress,
                    "candidate",
                    experiment_id=record.experiment_id,
                    candidate=record.candidate,
                    claim=request.claim,
                )
            else:
                request = load_research_request(
                    experiment / "request.public.json",
                    manifest=manifest,
                )

            if record.state == "CANDIDATE_FROZEN":
                if record.public_checks_passed is None:
                    _notify(
                        progress,
                        "public_checks",
                        experiment_id=record.experiment_id,
                    )
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
                    _notify(
                        progress,
                        "public_checks_complete",
                        experiment_id=record.experiment_id,
                        passed=record.public_checks_passed,
                    )
                if record.public_checks_passed is False:
                    from .experiment import publish_candidate_rejection

                    result = publish_candidate_rejection(task.config, experiment, request)
                    results.append(result)
                    _record_official_result(task, result, manifest)
                    _notify(progress, "result", result=result)
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
        _notify(
            progress,
            "comparison",
            experiment_id=record.experiment_id,
            kind="primary",
            trial_count=trial_count,
        )
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
                progress=progress,
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
                _notify(
                    progress,
                    "provisional",
                    experiment_id=record.experiment_id,
                )
                _notify(
                    progress,
                    "comparison",
                    experiment_id=record.experiment_id,
                    kind="suspect",
                    trial_count=trial_count,
                )
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
                        progress=progress,
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
                        progress=progress,
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
        if load_experiment(experiment).state == "REFLECTING":
            try:
                _reflect_final_result(
                    task,
                    experiment,
                    manifest,
                    result,
                    command_builder=reflection_command_builder,
                    stop_path=stop,
                    progress=progress,
                )
            except StoppedError:
                stop.unlink(missing_ok=True)
                stopped = True
                break
            except StateError:
                _mark_reflection_failed(experiment)
                reflection_failed = True
                _notify(
                    progress,
                    "reflection_failed",
                    experiment_id=record.experiment_id,
                )
                break
        _record_official_result(task, result, manifest)
        _notify(progress, "result", result=result)
        _remove_experiment_worktrees(task, record.experiment_id)
        if stop.exists():
            stop.unlink()
            stopped = True
            break
    _notify(
        progress,
        "complete",
        experiments=len(results),
        stopped=stopped,
    )
    limit_reached = (
        _completed_experiment_count(task.directory) >= task.config.max_experiments
    )
    return RunOutcome(
        tuple(results), stopped, stalled, reflection_failed, limit_reached
    )
