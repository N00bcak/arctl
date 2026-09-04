"""Persistent single-task experiment orchestration."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from itertools import count
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

from .agent_backend import (
    AgentSessionRequest,
    agent_command,
    agent_environment,
    agent_provenance,
)
from .agent_selection import select_agent
from .approval import verify_approval
from .bytecode import ensure_experiment_bytecode_cache
from .calibration import CalibrationCommandBuilder, calibrate_trial_count
from .candidate_review import AgentCommandBuilder as ReviewCommandBuilder
from .candidate_review import requirement_audit_schema, review_candidate
from .codex_schema import validate_codex_output_schema
from .comparison import ComparisonReservation, load_reservation, reserve_comparison
from .comparison_run import (
    SUBJECT_WORKERS,
    CommandBuilder,
    ComparisonFailure,
    run_comparison,
)
from .components import invoke_component
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
    normalize_runtime_artifacts,
    remove_worktree,
    resolve_commit,
)
from .manifest import EvaluatorManifest
from .models import Evidence, ResearchRequest
from .process import run_or_load_once
from .reflection import ReflectionCommandBuilder, validate_reflection
from .registry import LocatedTask
from .results import normalize_result_statuses, validate_public_result
from .sandbox import (
    agent_prompt_path,
    command_runtime_read_paths,
    marked_command,
    research_command,
    sandbox_command,
    sanitized_environment,
)
from .storage import atomic_write_text, write_json_once
from .search import (
    AgentCommandBuilder,
    add_ledger_entry,
    load_ledger,
    next_search_id,
    planning_schema,
    rebuild_catalog,
    record_miss,
    research_schema,
    validate_research_links,
    validate_planning,
)
from .seeds import load_setup_preflight_seeds
from .taskio import load_manifest
from .trials import load_trial_count, load_trial_count_record

ResearchCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]
PublicCheckCommandBuilder = Callable[[Sequence[str], Path, Path], Sequence[str]]
ProgressCallback = Callable[[dict[str, Any]], None]
COMPUTE_PROBE_HEADROOM = 0.8
_DEFAULT_STRATEGY = object()


def _notify(
    progress: ProgressCallback | None,
    event: str,
    **fields: Any,
) -> None:
    if progress is not None:
        progress({"event": event, **fields})


def _validate_codex_output_schema(schema: Mapping[str, Any]) -> None:
    validate_codex_output_schema(schema)


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
    reflection_error: str | None = None


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
            name: asdict(metric) for name, metric in manifest.public_telemetry.items()
        },
        "strategy": strategy,
        "exploration_catalog": str(
            task.directory / "exploration" / "ledger.public.jsonl"
        ),
        "exploration_entries": str(task.directory / "exploration" / "entries"),
        "candidate_review_contract": (
            task.config.candidate_review.contract
            if task.config.candidate_review is not None
            else None
        ),
    }
    return (
        "Make one focused improvement to the checked-out champion for this public "
        "research task. First select one successful-policy behavior from the strategy "
        "and name its id as strategy_behavior_id. Then propose a concrete policy "
        "mechanism, reason about viability, and search prior results, telemetry, and "
        "reflections through the compact public catalog before opening only the "
        "canonical history entries relevant to the decision. "
        "Prefer material mechanisms; substantiate numeric-only changes with a public "
        "sweep. Respect the candidate review contract and editable paths, do not commit, "
        "and return only the required research-request JSON object.\n\n"
        + json.dumps(packet, sort_keys=True, separators=(",", ":"))
    )


def _run_planning(
    task: LocatedTask,
    attempt: Path,
    worktree: Path,
    manifest: EvaluatorManifest,
    *,
    command_builder: AgentCommandBuilder | None,
    stop_path: Path,
) -> dict[str, Any] | None:
    assert task.config.method is not None
    task.config.method.require_component("plan", "plan.comparative-v1")
    rebuild_catalog(task.directory)
    strategy_files = sorted((task.directory / "strategy").glob("*.public.json"))
    strategy = json.loads(strategy_files[-1].read_text(encoding="utf-8"))
    ledger = load_ledger(task.directory)
    scratch = attempt / "planning" / "output"
    scratch.mkdir(parents=True, exist_ok=True)
    behavior_ids = tuple(item["id"] for item in strategy["successful_policy_behaviors"])
    ledger_ids = tuple(entry["entry_id"] for entry in ledger)
    schema_value = planning_schema(
        manifest,
        behavior_ids=behavior_ids,
        ledger_ids=ledger_ids,
    )
    schema = scratch / "planning.schema.json"
    write_json_once(schema, schema_value)
    packet = {
        "objective": task.config.objective,
        "editable_paths": list(task.config.editable_paths),
        "public_checks": [list(command) for command in task.config.public_checks],
        "public_probe": list(task.config.public_probe),
        "statistic": manifest.public_statistic,
        "telemetry": {
            name: asdict(metric) for name, metric in manifest.public_telemetry.items()
        },
        "strategy": strategy,
        "exploration_catalog": str(
            task.directory / "exploration" / "ledger.public.jsonl"
        ),
        "exploration_entries": str(task.directory / "exploration" / "entries"),
        "candidate_review_contract": (
            task.config.candidate_review.contract
            if task.config.candidate_review is not None
            else None
        ),
    }
    prompt = (
        "Plan the next experiment without editing the current champion. Assess every "
        "successful-policy behavior exactly once against the champion and public "
        "exploration catalog. Search the catalog, then open only relevant canonical "
        "entries. For each direction, describe what the champion expresses and the "
        "remaining gap, then attach one complete research request or mark the direction "
        "exhausted. The top-level selection must contain only the chosen strategy "
        "behavior id; the selected direction's request is the sole frozen request. "
        "Requests may specify only policy changes within editable paths: never prescribe "
        "trial counts, seeds, calibration, statistical thresholds, evaluator changes, "
        "or other controller-owned protocol. It is valid to exhaust every direction; "
        "never invent a weak candidate merely to continue. An untested experiment cannot "
        "support or contradict performance and cannot by itself exhaust a performance "
        "direction. After comparing all "
        "directions, select the best request or set selection to null when all are "
        "exhausted. A numeric-only change requires a deliberate public sweep. Do not "
        "edit, commit, or run hidden evaluation. Return only the required planning JSON."
        "\n\n" + json.dumps(packet, sort_keys=True, separators=(",", ":"))
    )
    planning_root = attempt / "planning"
    session_paths = sorted((planning_root / "attempts").glob("[0-9]" * 4))
    session = planning_root / "attempts" / f"{len(session_paths) + 1:04d}"
    if command_builder is None:
        exploration = task.directory / "exploration"
        assert task.config.method is not None
        agent = select_agent(
            task.config.method,
            component="plan",
            lifecycle=f"planning:{attempt.parent.parent.name}:{attempt.name}",
            root=planning_root,
        )
        command = agent_command(
            agent,
            AgentSessionRequest(
                worktree=worktree,
                scratch=scratch,
                output_schema=schema,
                prompt=prompt,
                read_paths=((exploration,) if exploration.is_dir() else ()),
                output_name="planning.public.json",
                writable_worktree=False,
            ),
        )
        environment = agent_environment(
            agent,
            credential_home=Path(
                os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
            ),
            writable_home=scratch,
        )
        write_json_once(
            session / "agent.public.json",
            agent_provenance(
                agent,
                lifecycle=f"planning:{attempt.parent.parent.name}:{attempt.name}",
            ),
        )
    else:
        command = command_builder(worktree, scratch, schema, prompt)
        environment = None
    process = session / "process"
    result = run_or_load_once(
        process,
        command,
        timeout_seconds=3600,
        max_output_bytes=2_000_000,
        cwd=worktree,
        env=environment,
        stop_path=stop_path,
        stdin_path=(
            agent_prompt_path(scratch)
            if command_builder is None and agent_prompt_path(scratch).is_file()
            else None
        ),
    )
    if result["return_code"] != 0:
        transient = transient_process_error(
            process, stage="planning", codex=command_builder is None
        )
        if transient is not None:
            raise transient
        raise StateError("fresh planning session exited unsuccessfully")
    try:
        value = json.loads(
            (scratch / "planning.public.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema_value).validate(value)
        if not isinstance(value, dict):
            raise ValidationError("planning output is not an object")
        selected = validate_planning(
            value, strategy=strategy, ledger=ledger, manifest=manifest
        )
    except (OSError, json.JSONDecodeError, JsonSchemaError, ValidationError) as error:
        raise ResearchMiss("invalid_plan", str(error)) from error
    write_json_once(attempt / "planning.public.json", value)
    if selected is not None:
        write_json_once(attempt / "request.public.json", selected)
    return selected


def _implementation_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "status",
            "summary",
            "deviations",
            "requirements",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": 2},
            "status": {"type": "string", "enum": ["implemented", "infeasible"]},
            "summary": text,
            "deviations": {"type": "array", "items": text},
            "requirements": requirement_audit_schema(),
        },
    }


def _validate_implementation_report(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("schema_version") == 1:
        legacy = {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "status", "summary", "deviations"],
            "properties": {
                "schema_version": {"type": "integer", "const": 1},
                "status": {
                    "type": "string",
                    "enum": ["implemented", "infeasible"],
                },
                "summary": {"type": "string", "minLength": 1},
                "deviations": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        }
        Draft202012Validator(legacy).validate(value)
        return value
    Draft202012Validator(_implementation_schema()).validate(value)
    assert isinstance(value, dict)
    if value["status"] == "implemented" and any(
        item["status"] != "verified" for item in value["requirements"]
    ):
        raise ValidationError("completed implementation has unverified requirements")
    return value


def _run_implementation(
    task: LocatedTask,
    manifest: EvaluatorManifest,
    attempt: Path,
    worktree: Path,
    request: Mapping[str, Any],
    *,
    trial_count: int,
    command_builder: ResearchCommandBuilder | None,
    stop_path: Path,
) -> dict[str, Any]:
    assert task.config.method is not None
    task.config.method.require_component("execute", "execute.worktree-v1")
    scratch = attempt / "implementation"
    scratch.mkdir(parents=True, exist_ok=True)
    schema_value = _implementation_schema()
    schema = scratch / "implementation.schema.json"
    write_json_once(schema, schema_value)
    prompt = (
        "Implement exactly the frozen experiment brief in the checked-out champion. "
        "Before editing, inspect the relevant trusted interface and extract every "
        "behavioral, fallback, validation, and fidelity obligation from the request. "
        "After editing, run applicable public checks and targeted probes, then re-read "
        "the request sentence by sentence against the final diff. Keep fixing until "
        "every requirement has concrete code or test evidence; generic tests alone do "
        "not verify mechanism-specific branches. Do not replace, broaden, or reinterpret "
        "the claim or mechanism. Stay within editable paths and respect the candidate-"
        "review contract. Return status implemented only when every checklist item is "
        "verified. If fidelity cannot be established, do not substitute an easier idea: "
        "report infeasible. Emit schema version 2, do not commit, and return only the "
        "required implementation JSON.\n\n"
        + json.dumps(
            {
                "request": request,
                "editable_paths": list(task.config.editable_paths),
                "denied_paths": list(task.config.denied_paths),
                "public_checks": [
                    list(command) for command in task.config.public_checks
                ],
                "public_probe": list(task.config.public_probe),
                "candidate_review_contract": (
                    task.config.candidate_review.contract
                    if task.config.candidate_review is not None
                    else None
                ),
                "subject_interface": manifest.subject_interface,
                "compute_envelope": {
                    "official_trials": trial_count,
                    "timeout_seconds": manifest.limits.timeout_seconds,
                    "public_probe": list(task.config.public_probe),
                    "probe_trial_equivalents": task.config.public_probe_trial_equivalents,
                    "advisory_only": True,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    implementation_root = attempt / "implementation"
    session_paths = sorted((implementation_root / "attempts").glob("[0-9]" * 4))
    session = implementation_root / "attempts" / f"{len(session_paths) + 1:04d}"
    if command_builder is None:
        assert task.config.method is not None
        agent = select_agent(
            task.config.method,
            component="execute",
            lifecycle=f"implementation:{attempt.parent.parent.name}:{attempt.name}",
            root=implementation_root,
        )
        command = agent_command(
            agent,
            AgentSessionRequest(
                worktree=worktree,
                scratch=scratch,
                output_schema=schema,
                prompt=prompt,
                output_name="implementation.public.json",
                writable_worktree=True,
                read_paths=tuple(
                    dict.fromkeys(
                        path
                        for public_command in (
                            *task.config.public_checks,
                            task.config.public_probe,
                        )
                        for path in command_runtime_read_paths(public_command)
                        if not path.is_relative_to(worktree)
                    )
                ),
            ),
        )
        environment = agent_environment(
            agent,
            credential_home=Path(
                os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
            ),
            writable_home=scratch,
        )
        write_json_once(
            session / "agent.public.json",
            agent_provenance(
                agent,
                lifecycle=f"implementation:{attempt.parent.parent.name}:{attempt.name}",
            ),
        )
    else:
        command = command_builder(worktree, scratch, schema, prompt)
        environment = None
    process = session / "process"
    result = run_or_load_once(
        process,
        command,
        timeout_seconds=3600,
        max_output_bytes=2_000_000,
        cwd=worktree,
        env=environment,
        stop_path=stop_path,
        stdin_path=(
            agent_prompt_path(scratch)
            if command_builder is None and agent_prompt_path(scratch).is_file()
            else None
        ),
    )
    if result["return_code"] != 0:
        transient = transient_process_error(
            process, stage="implementation", codex=command_builder is None
        )
        if transient is not None:
            raise transient
        raise StateError("fresh implementation session exited unsuccessfully")
    try:
        raw = json.loads(
            (scratch / "implementation.public.json").read_text(encoding="utf-8")
        )
        value = _validate_implementation_report(raw)
    except (
        OSError,
        json.JSONDecodeError,
        JsonSchemaError,
        ValidationError,
    ) as error:
        raise ResearchMiss(
            "invalid_implementation", "invalid implementation report"
        ) from error
    if value["status"] == "infeasible":
        raise ResearchMiss("implementation_infeasible", value["summary"])
    if value["deviations"]:
        raise ResearchMiss(
            "implementation_deviated",
            "implementation reported deviations from the frozen brief",
        )
    write_json_once(attempt / "implementation.public.json", value)
    return value


def _load_implementation_report(
    attempt: Path,
) -> dict[str, Any] | None:
    path = attempt / "implementation.public.json"
    if not path.is_file():
        return None
    try:
        return _validate_implementation_report(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, JsonSchemaError, ValidationError) as error:
        raise StateError("saved implementation report is invalid") from error


def _run_compute_probe(
    task: LocatedTask,
    manifest: EvaluatorManifest,
    *,
    attempt: Path,
    worktree: Path,
    trial_count: int,
    command_builder: PublicCheckCommandBuilder | None,
    stop_path: Path,
) -> dict[str, Any]:
    def unavailable_reason(command: Sequence[str]) -> str | None:
        compile_modules = {"compileall", "py_compile"}
        executable = Path(command[0]).name if command else ""
        if executable in compile_modules:
            return "compile_only_probe"
        for index, argument in enumerate(command[:-1]):
            if argument == "-m" and command[index + 1] in compile_modules:
                return "compile_only_probe"
        return None

    def normalized_report(value: Mapping[str, Any]) -> dict[str, Any]:
        reason = unavailable_reason(task.config.public_probe)
        if value.get("schema_version") == 1:
            value = {
                **value,
                "schema_version": 2,
                "command": list(task.config.public_probe),
                "measurement_basis": "declared_paired_trial_equivalents",
                "unavailable_reason": None,
            }
        fields = {
            "schema_version",
            "status",
            "command",
            "measurement_basis",
            "unavailable_reason",
            "elapsed_seconds",
            "trial_equivalents",
            "official_trials",
            "projected_seconds",
            "timeout_seconds",
            "headroom_fraction",
            "risk",
            "advisory_only",
        }
        if (
            set(value) != fields
            or value["schema_version"] != 2
            or value["status"] not in {"available", "unavailable"}
            or value["command"] != list(task.config.public_probe)
            or value["measurement_basis"] != "declared_paired_trial_equivalents"
            or value["risk"]
            not in {"within_advisory_budget", "likely_over_budget", "unavailable"}
            or value["official_trials"] != trial_count
            or value["trial_equivalents"] != task.config.public_probe_trial_equivalents
            or value["timeout_seconds"] != manifest.limits.timeout_seconds
            or value["headroom_fraction"] != COMPUTE_PROBE_HEADROOM
            or value["advisory_only"] is not True
        ):
            raise StateError("saved compute probe is invalid")
        if reason is not None:
            return {
                **value,
                "status": "unavailable",
                "unavailable_reason": reason,
                "elapsed_seconds": None,
                "projected_seconds": None,
                "risk": "unavailable",
            }
        return dict(value)

    published = attempt / "compute-probe.public.json"
    if published.is_file():
        try:
            value = json.loads(published.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("saved compute probe is invalid") from error
        if not isinstance(value, dict):
            raise StateError("saved compute probe is invalid")
        return normalized_report(value)
    root = attempt / "compute-probe"
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "execution.started"
    reason = unavailable_reason(task.config.public_probe)
    if reason is not None:
        elapsed = None
        available = False
    else:
        if command_builder is None:
            command = sandbox_command(
                marked_command(task.config.public_probe, marker),
                cwd=worktree,
                read_paths=command_runtime_read_paths(task.config.public_probe),
                write_paths=(worktree, root),
                profile="arctl-research",
            )
            environment = sanitized_environment(
                codex_home=root / "codex-home",
                writable_home=root / "home",
            )
        else:
            command = command_builder(task.config.public_probe, worktree, root)
            environment = None
        try:
            process = run_or_load_once(
                root / "process",
                command,
                timeout_seconds=manifest.limits.timeout_seconds,
                max_output_bytes=manifest.limits.max_output_bytes,
                cwd=worktree,
                env=environment,
                stop_path=stop_path,
            )
            elapsed = process.get("elapsed_seconds")
            available = process["return_code"] == 0 and isinstance(
                elapsed, (int, float)
            )
            if not available:
                reason = "probe_execution_failed"
        except StoppedError:
            raise
        except ProcessError:
            elapsed = None
            available = False
            reason = "probe_execution_failed"
    equivalents = task.config.public_probe_trial_equivalents
    if equivalents is None and reason is None:
        reason = "trial_equivalents_undeclared"
    projected = (
        float(elapsed) * trial_count / equivalents
        if available and equivalents is not None
        else None
    )
    risk = (
        "likely_over_budget"
        if projected is not None
        and projected > manifest.limits.timeout_seconds * COMPUTE_PROBE_HEADROOM
        else "within_advisory_budget" if projected is not None else "unavailable"
    )
    value = {
        "schema_version": 2,
        "status": "available" if available else "unavailable",
        "command": list(task.config.public_probe),
        "measurement_basis": "declared_paired_trial_equivalents",
        "unavailable_reason": reason,
        "elapsed_seconds": elapsed,
        "trial_equivalents": equivalents,
        "official_trials": trial_count,
        "projected_seconds": projected,
        "timeout_seconds": manifest.limits.timeout_seconds,
        "headroom_fraction": COMPUTE_PROBE_HEADROOM,
        "risk": risk,
        "advisory_only": True,
    }
    write_json_once(published, value)
    return normalized_report(value)


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
        exploration = task.directory / "exploration"
        if exploration.is_dir():
            runtime_paths.append(exploration)
        command = research_command(
            worktree=worktree,
            scratch=scratch,
            output_schema=schema,
            prompt=prompt,
            read_paths=tuple(
                dict.fromkeys(
                    path for path in runtime_paths if not path.is_relative_to(worktree)
                )
            ),
            model=task.config.execution_model,
            reasoning_effort=task.config.execution_reasoning_effort,
        )
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
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
                stdin_path=(
                    agent_prompt_path(scratch)
                    if command_builder is _default_research_command
                    else None
                ),
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
                "fresh research session did not write valid request JSON",
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
    trial_count: int,
    research_command_builder: ResearchCommandBuilder,
    planning_command_builder: AgentCommandBuilder | None,
    strategy_command_builder: AgentCommandBuilder | None,
    review_command_builder: ReviewCommandBuilder | None,
    repair_command_builder: ReviewCommandBuilder | None,
    check_command_builder: PublicCheckCommandBuilder | None,
    stop_path: Path,
    progress: ProgressCallback | None,
) -> tuple[Path, str, dict[str, Any], Path] | None:
    """Return one novel controller-created candidate."""
    recovered = _recover_candidate_stage(
        task,
        manifest,
        champion=champion,
        trial_count=trial_count,
        planning_command_builder=planning_command_builder,
        research_command_builder=research_command_builder,
        review_command_builder=review_command_builder,
        repair_command_builder=repair_command_builder,
        check_command_builder=check_command_builder,
        stop_path=stop_path,
        progress=progress,
    )
    if recovered is not None:
        return recovered
    recovered = _recover_candidate_review(
        task,
        manifest,
        champion=champion,
        trial_count=trial_count,
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
    strategy_worktree = (
        task.directory / "worktrees" / f"search-{search_id:06d}-strategy"
    )
    _checkout(task.config.repo, strategy_worktree, champion)
    try:
        revision, strategy_value = invoke_component(
            "strategize",
            task.config.method.components["strategize"].identifier,
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

    split_roles = (
        research_command_builder is _default_research_command
        or planning_command_builder is not None
    )
    attempts = range(1, 1_000_000_000) if split_roles else range(1, 7)
    for attempt in attempts:
        if attempt == 4 and not split_roles:
            _checkout(task.config.repo, strategy_worktree, champion)
            try:
                revision, _ = invoke_component(
                    "strategize",
                    task.config.method.components["strategize"].identifier,
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
        worktree = (
            task.directory
            / "worktrees"
            / f"search-{search_id:06d}-attempt-{attempt:02d}"
        )
        _notify(
            progress,
            "search_attempt",
            search_id=search_id,
            attempt=attempt,
            attempts=None if split_roles else 6,
        )
        _checkout(task.config.repo, worktree, champion)
        implementation_report: dict[str, Any] | None = None
        try:
            if split_roles:
                try:
                    request = invoke_component(
                        "plan",
                        task.config.method.components["plan"].identifier,
                        task,
                        attempt_directory,
                        worktree,
                        manifest,
                        command_builder=planning_command_builder,
                        stop_path=stop_path,
                    )
                except (StateError, TransientDownstreamError) as error:
                    write_json_once(
                        attempt_directory / "planning.failure.json",
                        {"schema_version": 1, "message": str(error)},
                    )
                    raise
                _notify(progress, "planning", selected=request is not None)
                if request is None:
                    miss = ResearchMiss(
                        "directions_exhausted",
                        "planner exhausted every current strategic direction",
                    )
                    record_miss(
                        task,
                        search_id=search_id,
                        attempt=attempt,
                        champion=champion,
                        request=None,
                        miss=miss,
                        planning=json.loads(
                            (attempt_directory / "planning.public.json").read_text(
                                encoding="utf-8"
                            )
                        ),
                    )
                    _notify(
                        progress,
                        "search_miss",
                        attempt=attempt,
                        code=miss.code,
                        message=str(miss),
                    )
                    remove_worktree(task.config.repo, worktree)
                    while True:
                        _checkout(task.config.repo, strategy_worktree, champion)
                        try:
                            revision, refreshed = invoke_component(
                                "strategize",
                                task.config.method.components["strategize"].identifier,
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
                        if refreshed != strategy_value:
                            strategy_value = refreshed
                            break
                        add_ledger_entry(
                            task.directory,
                            {
                                "schema_version": 1,
                                "source": f"strategy-miss:{revision:06d}",
                                "kind": "strategy_miss",
                                "strategy_revision": revision,
                                "rejection_code": "exact_duplicate_strategy",
                                "message": (
                                    "refreshed strategy exactly duplicated the "
                                    "exhausted strategy"
                                ),
                            },
                        )
                    continue
                try:
                    implementation_report = invoke_component(
                        "execute",
                        task.config.method.components["execute"].identifier,
                        task,
                        manifest,
                        attempt_directory,
                        worktree,
                        request,
                        trial_count=trial_count,
                        command_builder=(
                            None
                            if research_command_builder is _default_research_command
                            else research_command_builder
                        ),
                        stop_path=stop_path,
                    )
                except (StateError, TransientDownstreamError) as error:
                    write_json_once(
                        attempt_directory / "implementation.failure.json",
                        {"schema_version": 1, "message": str(error)},
                    )
                    raise
            else:
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
            normalize_runtime_artifacts(
                worktree,
                stage="implementation" if split_roles else "research",
                audit_path=attempt_directory / "runtime-artifacts.public.json",
            )
            if not isinstance(request, dict):
                raise ResearchMiss(
                    "invalid_request", "research request is not an object"
                )
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
            compute_report = _run_compute_probe(
                task,
                manifest,
                attempt=attempt_directory,
                worktree=worktree,
                trial_count=trial_count,
                command_builder=check_command_builder,
                stop_path=stop_path,
            )
            normalize_runtime_artifacts(
                worktree,
                stage="compute-probe",
                audit_path=attempt_directory / "runtime-artifacts.public.json",
            )
            _notify(progress, "compute_probe", report=compute_report)
            review_candidate(
                task,
                manifest,
                worktree=worktree,
                attempt_directory=attempt_directory,
                champion=champion,
                request=request,
                stop_path=stop_path,
                implementation_report=implementation_report,
                compute_report=compute_report,
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
                runtime_artifact_audit=(
                    attempt_directory / "runtime-artifacts.public.json"
                ),
            )
        except StoppedError:
            remove_worktree(task.config.repo, worktree)
            raise
        except TransientDownstreamError as error:
            if error.stage in ("execution", "planning", "implementation"):
                remove_worktree(task.config.repo, worktree)
            raise
        except ResearchMiss as miss:
            try:
                raw = json.loads(
                    (attempt_directory / "request.public.json").read_text(
                        encoding="utf-8"
                    )
                )
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
            _notify(
                progress,
                "search_miss",
                attempt=attempt,
                code=miss.code,
                message=str(miss),
            )
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


def _recover_candidate_stage(
    task: LocatedTask,
    manifest: EvaluatorManifest,
    *,
    champion: str,
    trial_count: int,
    planning_command_builder: AgentCommandBuilder | None,
    research_command_builder: ResearchCommandBuilder,
    review_command_builder: ReviewCommandBuilder | None,
    repair_command_builder: ReviewCommandBuilder | None,
    check_command_builder: PublicCheckCommandBuilder | None,
    stop_path: Path,
    progress: ProgressCallback | None,
) -> tuple[Path, str, dict[str, Any], Path] | None:
    """Retry a failed planning or implementation lifecycle with its saved draw."""
    searches = sorted((task.directory / "searches").glob("[0-9]" * 6), reverse=True)
    for search in searches:
        attempts = sorted((search / "attempts").glob("[0-9][0-9]"), reverse=True)
        for attempt_directory in attempts:
            if (
                (attempt_directory / "candidate-review").is_dir()
                or (attempt_directory / "candidate.public.json").is_file()
                or (attempt_directory / "recovery.complete").is_file()
            ):
                continue
            planning_failed = (attempt_directory / "planning.failure.json").is_file()
            implementation_failed = (
                attempt_directory / "implementation.failure.json"
            ).is_file()
            if not (planning_failed or implementation_failed):
                continue
            search_id = int(search.name)
            attempt_number = int(attempt_directory.name)
            worktree = (
                task.directory
                / "worktrees"
                / f"search-{search_id:06d}-attempt-{attempt_number:02d}"
            )
            if worktree.exists():
                remove_worktree(task.config.repo, worktree)
            _checkout(task.config.repo, worktree, champion)
            try:
                if (
                    planning_failed
                    and not (attempt_directory / "request.public.json").is_file()
                ):
                    request = invoke_component(
                        "plan",
                        task.config.method.components["plan"].identifier,
                        task,
                        attempt_directory,
                        worktree,
                        manifest,
                        command_builder=planning_command_builder,
                        stop_path=stop_path,
                    )
                    if request is None:
                        atomic_write_text(attempt_directory / "recovery.complete", "\n")
                        remove_worktree(task.config.repo, worktree)
                        return None
                else:
                    request = json.loads(
                        (attempt_directory / "request.public.json").read_text(
                            encoding="utf-8"
                        )
                    )
                if not isinstance(request, dict):
                    raise StateError("recoverable candidate request is invalid")
                try:
                    implementation_report = invoke_component(
                        "execute",
                        task.config.method.components["execute"].identifier,
                        task,
                        manifest,
                        attempt_directory,
                        worktree,
                        request,
                        trial_count=trial_count,
                        command_builder=(
                            None
                            if research_command_builder is _default_research_command
                            else research_command_builder
                        ),
                        stop_path=stop_path,
                    )
                except (StateError, TransientDownstreamError) as error:
                    failure_path = attempt_directory / "implementation.failure.json"
                    if not failure_path.exists():
                        write_json_once(
                            failure_path,
                            {"schema_version": 1, "message": str(error)},
                        )
                    raise
            except (StateError, TransientDownstreamError):
                remove_worktree(task.config.repo, worktree)
                raise
            normalize_runtime_artifacts(
                worktree,
                stage="implementation",
                audit_path=attempt_directory / "runtime-artifacts.public.json",
            )
            ResearchRequest.from_mapping(
                request, allowed_telemetry=manifest.public_telemetry
            )
            strategy_files = sorted((task.directory / "strategy").glob("*.public.json"))
            strategy = json.loads(strategy_files[-1].read_text(encoding="utf-8"))
            validate_research_links(
                request, strategy=strategy, ledger=load_ledger(task.directory)
            )
            compute_report = _run_compute_probe(
                task,
                manifest,
                attempt=attempt_directory,
                worktree=worktree,
                trial_count=trial_count,
                command_builder=check_command_builder,
                stop_path=stop_path,
            )
            normalize_runtime_artifacts(
                worktree,
                stage="compute-probe",
                audit_path=attempt_directory / "runtime-artifacts.public.json",
            )
            _notify(progress, "compute_probe", report=compute_report)
            review_candidate(
                task,
                manifest,
                worktree=worktree,
                attempt_directory=attempt_directory,
                champion=champion,
                request=request,
                stop_path=stop_path,
                implementation_report=implementation_report,
                compute_report=compute_report,
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
                message=f"arctl search {search_id} attempt {attempt_number}",
                runtime_artifact_audit=(
                    attempt_directory / "runtime-artifacts.public.json"
                ),
            )
            write_json_once(
                attempt_directory / "candidate.public.json",
                {
                    "schema_version": 1,
                    "candidate": candidate,
                    "changed_paths": list(changed_paths),
                },
            )
            return worktree, candidate, request, attempt_directory
    return None


def _recover_candidate_review(
    task: LocatedTask,
    manifest: EvaluatorManifest,
    *,
    champion: str,
    trial_count: int,
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
        attempts_root = search / "attempts"
        attempts = (
            sorted(
                (
                    path
                    for path in attempts_root.iterdir()
                    if path.is_dir() and path.name.isdigit()
                ),
                reverse=True,
            )
            if attempts_root.is_dir()
            else []
        )
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
                raise StateError(
                    "recoverable candidate review has invalid request"
                ) from error
            if not isinstance(request, dict):
                raise StateError("recoverable candidate review has invalid request")
            ResearchRequest.from_mapping(
                request,
                allowed_telemetry=manifest.public_telemetry,
            )
            strategy_files = sorted((task.directory / "strategy").glob("*.public.json"))
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
            normalize_runtime_artifacts(
                worktree,
                stage="candidate-review-recovery",
                audit_path=attempt_directory / "runtime-artifacts.public.json",
            )
            compute_report = _run_compute_probe(
                task,
                manifest,
                attempt=attempt_directory,
                worktree=worktree,
                trial_count=trial_count,
                command_builder=check_command_builder,
                stop_path=stop_path,
            )
            normalize_runtime_artifacts(
                worktree,
                stage="compute-probe",
                audit_path=attempt_directory / "runtime-artifacts.public.json",
            )
            _notify(progress, "compute_probe", report=compute_report)
            review_candidate(
                task,
                manifest,
                worktree=worktree,
                attempt_directory=attempt_directory,
                champion=champion,
                request=request,
                stop_path=stop_path,
                implementation_report=(_load_implementation_report(attempt_directory)),
                compute_report=compute_report,
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
                runtime_artifact_audit=(
                    attempt_directory / "runtime-artifacts.public.json"
                ),
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
    result = normalize_result_statuses(result)
    request_path = (
        task.directory
        / "experiments"
        / f"{int(result['experiment_id']):06d}"
        / "request.public.json"
    )
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(
            f"published research request is invalid: {request_path}"
        ) from error
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
        "operational_status": result["operational_status"],
        "scientific_status": result["scientific_status"],
        "reason_code": result["reason_code"],
        **(
            {"operational_assessment": result["operational_assessment"]}
            if result.get("operational_assessment")
            else {}
        ),
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
    subject_workers: int = SUBJECT_WORKERS,
    python_cache: Path | None = None,
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
        subject_workers=subject_workers,
        python_cache=python_cache,
        **arguments,
    )


def _reserved_seeds(task_directory: Path, *, excluding: Path) -> set[int]:
    seeds = load_setup_preflight_seeds(task_directory)
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
        if load_experiment(path).state == "COMPLETE" and (path / "published").is_file()
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


def _experiment_bytecode_cache(
    task: LocatedTask, experiment: Path, manifest: EvaluatorManifest
) -> Path:
    return ensure_experiment_bytecode_cache(
        task.directory,
        experiment,
        python_executable=manifest.subject_command[0],
    )


def _mini_gc_published_experiment(
    task: LocatedTask,
    experiment: Path,
    *,
    progress: ProgressCallback | None,
) -> bool:
    from .retention import run_experiment_gc

    try:
        result = run_experiment_gc(task.directory, experiment)
    except (OSError, StateError) as error:
        _notify(
            progress,
            "mini_gc_failed",
            experiment_id=int(experiment.name),
            message=str(error),
        )
        return False
    if result["failed"]:
        _notify(
            progress,
            "mini_gc_failed",
            experiment_id=int(experiment.name),
            plan_hash=result["plan_hash"],
            message="experiment cleanup requires manual recovery",
        )
        return False
    _notify(
        progress,
        "mini_gc_complete",
        experiment_id=int(experiment.name),
        plan_hash=result["plan_hash"],
        reclaimed_bytes=result["reclaimed_bytes"],
    )
    return True


def _reflect_final_result(
    task: LocatedTask,
    experiment: Path,
    manifest: EvaluatorManifest,
    result: dict[str, Any],
    *,
    command_builder: ReflectionCommandBuilder | None,
    stop_path: Path,
    progress: ProgressCallback | None,
    python_cache: Path,
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
    assert task.config.method is not None
    invoke_component(
        "reflect",
        task.config.method.components["reflect"].identifier,
        task=task.config,
        experiment=experiment,
        manifest=manifest,
        request=request,
        result=result,
        candidate_worktree=candidate_worktree,
        champion_worktree=champion_worktree,
        stop_path=stop_path,
        command_builder=command_builder,
        python_cache=python_cache,
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
    planning_command_builder: AgentCommandBuilder | None = None,
    strategy_command_builder: AgentCommandBuilder | None | object = _DEFAULT_STRATEGY,
    review_command_builder: ReviewCommandBuilder | None = None,
    repair_command_builder: ReviewCommandBuilder | None = None,
    public_check_command_builder: PublicCheckCommandBuilder | None = None,
    comparison_command_builder: CommandBuilder | None = None,
    calibration_command_builder: CalibrationCommandBuilder | None = None,
    reflection_command_builder: ReflectionCommandBuilder | None = None,
    progress: ProgressCallback | None = None,
    subject_workers: int = SUBJECT_WORKERS,
) -> RunOutcome:
    """Run a bounded sequence of fixed-trial experiments for one approved task."""
    if strategy_command_builder is _DEFAULT_STRATEGY:
        strategy_command_builder = (
            None
            if research_command_builder is _default_research_command
            else _compatibility_strategy_command
        )
    if (
        isinstance(subject_workers, bool)
        or not isinstance(subject_workers, int)
        or not 1 <= subject_workers <= SUBJECT_WORKERS
    ):
        raise StateError(f"workers must be between 1 and {SUBJECT_WORKERS}")
    approval = verify_approval(task.directory, task.config)
    assert task.config.method is not None
    task.config.method.require_component("search", "search.serial-champion-v1")
    task.config.method.require_component("evaluate", "evaluate.paired-suspect-v1")
    manifest, manifest_hash = load_manifest(task.directory / "evaluator.manifest.json")
    for published in _public_history(task, manifest):
        _record_official_result(task, published, manifest)
    rebuild_catalog(task.directory)
    mini_gc_enabled = True
    from .retention import recover_gc_transaction

    try:
        recovered_gc = recover_gc_transaction(task.directory)
    except (OSError, StateError) as error:
        recovered_gc = None
        mini_gc_enabled = False
        _notify(progress, "mini_gc_failed", message=str(error))
    if recovered_gc is not None and recovered_gc["failed"]:
        mini_gc_enabled = False
        _notify(
            progress,
            "mini_gc_failed",
            plan_hash=recovered_gc["plan_hash"],
            message="incomplete cleanup requires manual recovery",
        )
    if mini_gc_enabled:
        experiments_root = task.directory / "experiments"
        for completed in (
            sorted(experiments_root.glob("[0-9]" * 6))
            if experiments_root.is_dir()
            else ()
        ):
            cache_record = completed / "runtime" / "bytecode-cache.private.json"
            cache_roots = completed / "runtime" / "python-bytecode"
            if (
                load_experiment(completed).state == "COMPLETE"
                and (completed / "published").is_file()
                and cache_record.is_file()
                and cache_roots.exists()
            ):
                mini_gc_enabled = _mini_gc_published_experiment(
                    task, completed, progress=progress
                )
                if not mini_gc_enabled:
                    break
    limit = task.config.max_experiments if max_experiments is None else max_experiments
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise StateError("max experiments must be a positive integer")
    if task.config.max_experiments is not None:
        remaining = task.config.max_experiments - _completed_experiment_count(
            task.directory
        )
        limit = max(remaining, 0) if limit is None else min(limit, max(remaining, 0))

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
        calibration_arguments["subject_workers"] = subject_workers
        if calibration_command_builder is not None:
            calibration_arguments["command_builder"] = calibration_command_builder
        calibration_champion = task.directory / "worktrees" / "calibration-champion"
        if manifest.calibration.controller_pilot:
            champion = approval["approved_champion"]
            if calibration_champion.exists() and resolve_commit(
                calibration_champion, "HEAD"
            ) != resolve_commit(task.config.repo, champion):
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
    reflection_error = None
    for _ in range(limit) if limit is not None else count():
        stop = task.directory / "stop.requested"
        active = _active_experiment(task.directory)
        if stop.exists():
            if active is not None:
                record = load_experiment(active)
                primary_reservation = (
                    active / "comparisons" / "primary" / "reservation.private.json"
                )
                if not primary_reservation.is_file():
                    if record.state == "RESEARCHING":
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
                    stopped_result = publish_comparison_failure(
                        task.config,
                        active,
                        request,
                        manifest,
                        source="stop",
                        primary=primary,
                    )
                    results.append(stopped_result)
                    _record_official_result(task, stopped_result, manifest)
                    _notify(progress, "result", result=stopped_result)
                    _remove_experiment_worktrees(task, record.experiment_id)
                    if mini_gc_enabled:
                        mini_gc_enabled = _mini_gc_published_experiment(
                            task, active, progress=progress
                        )
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
                    python_cache=_experiment_bytecode_cache(
                        task, experiment, manifest
                    ),
                )
            except StoppedError:
                stop.unlink(missing_ok=True)
                stopped = True
                break
            except (OSError, json.JSONDecodeError, StateError) as error:
                _mark_reflection_failed(experiment)
                reflection_failed = True
                reflection_error = str(error)
                _notify(
                    progress,
                    "reflection_failed",
                    experiment_id=load_experiment(experiment).experiment_id,
                    message=reflection_error,
                )
                break
            results.append(result_value)
            _record_official_result(task, result_value, manifest)
            _notify(progress, "result", result=result_value)
            _remove_experiment_worktrees(
                task, load_experiment(experiment).experiment_id
            )
            if mini_gc_enabled:
                mini_gc_enabled = _mini_gc_published_experiment(
                    task, experiment, progress=progress
                )
            continue
        if experiment is not None and (experiment / "research.failure.json").is_file():
            _discard_unreserved_experiment(task, experiment)
            experiment = None
        presearched = False
        if experiment is None:
            champion = resolve_commit(
                task.config.repo,
                f"refs/arctl/{task.config.task_id}/champion",
            )
            try:
                assert task.config.method is not None
                found = invoke_component(
                    "search",
                    task.config.method.components["search"].identifier,
                    task,
                    manifest,
                    champion=champion,
                    trial_count=trial_count,
                    research_command_builder=research_command_builder,
                    planning_command_builder=planning_command_builder,
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
            if task.config.schema_version >= 4:
                write_json_once(
                    experiment / "branch.public.json",
                    {
                        "schema_version": 1,
                        "search_component": task.config.method.components[
                            "search"
                        ].identifier,
                        "generation_id": record.experiment_id,
                        "branch_id": f"serial-{record.experiment_id:06d}",
                        "parent_branch_id": None,
                        "seed_policy": champion,
                        "official_champion": champion,
                    },
                )
            write_json_once(experiment / "request.public.json", raw_request)
            atomic_write_text(experiment / "candidate.commit", candidate + "\n")
            for name in ("planning.public.json", "implementation.public.json"):
                source = search_attempt / name
                if source.is_file():
                    shutil.copy2(source, experiment / name)
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

            python_cache = _experiment_bytecode_cache(task, experiment, manifest)

            if record.state == "CANDIDATE_FROZEN":
                if record.public_checks_passed is None:
                    _notify(
                        progress,
                        "public_checks",
                        experiment_id=record.experiment_id,
                    )
                    check_arguments: dict[str, Any] = {}
                    if public_check_command_builder is not None:
                        check_arguments["command_builder"] = (
                            public_check_command_builder
                        )
                    check_arguments["stop_path"] = stop
                    check_arguments["python_cache"] = python_cache
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

                    result = publish_candidate_rejection(
                        task.config, experiment, request
                    )
                    results.append(result)
                    _record_official_result(task, result, manifest)
                    _notify(progress, "result", result=result)
                    _remove_experiment_worktrees(task, record.experiment_id)
                    if mini_gc_enabled:
                        mini_gc_enabled = _mini_gc_published_experiment(
                            task, experiment, progress=progress
                        )
                    continue
        except StoppedError:
            if load_experiment(experiment).state == "RESEARCHING":
                _discard_unreserved_experiment(task, experiment)
            stop.unlink(missing_ok=True)
            stopped = True
            break
        except ArctlError:
            if (
                not (
                    experiment / "comparisons" / "primary" / "reservation.private.json"
                ).exists()
                and not (experiment / "result.public.json").exists()
                and not (experiment / "research.failure.json").exists()
                and not (experiment / "public-check.failure.json").exists()
            ):
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
            assert task.config.method is not None
            primary = invoke_component(
                "evaluate",
                task.config.method.components["evaluate"].identifier,
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
                subject_workers=subject_workers,
                python_cache=python_cache,
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
                cause=str(error),
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
                    suspect = invoke_component(
                        "evaluate",
                        task.config.method.components["evaluate"].identifier,
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
                        subject_workers=subject_workers,
                        python_cache=python_cache,
                    )
                except ComparisonFailure as error:
                    result = publish_comparison_failure(
                        task.config,
                        experiment,
                        request,
                        manifest,
                        source=error.source,
                        primary=primary,
                        cause=str(error),
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
                    invoke_component(
                        "evaluate",
                        task.config.method.components["evaluate"].identifier,
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
                        subject_workers=subject_workers,
                        python_cache=python_cache,
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
                    python_cache=python_cache,
                )
            except StoppedError:
                stop.unlink(missing_ok=True)
                stopped = True
                break
            except StateError as error:
                _mark_reflection_failed(experiment)
                reflection_failed = True
                reflection_error = str(error)
                _notify(
                    progress,
                    "reflection_failed",
                    experiment_id=record.experiment_id,
                    message=reflection_error,
                )
                break
        _record_official_result(task, result, manifest)
        _notify(progress, "result", result=result)
        _remove_experiment_worktrees(task, record.experiment_id)
        if mini_gc_enabled:
            mini_gc_enabled = _mini_gc_published_experiment(
                task, experiment, progress=progress
            )
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
    limit_reached = task.config.max_experiments is not None and (
        _completed_experiment_count(task.directory) >= task.config.max_experiments
    )
    return RunOutcome(
        tuple(results),
        stopped,
        stalled,
        reflection_failed,
        limit_reached,
        reflection_error,
    )
