"""Persistent experiment state, candidate freezing, checks, and publication."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from collections.abc import Callable, Sequence
from typing import Any, Literal, Mapping

from .decisions import Decision, failure_decision
from .dossier import ensure_experiment_dossier
from .errors import ProcessError, StateError, StoppedError, ValidationError
from .git import (
    create_candidate_commit,
    create_candidate_ref,
    create_detached_worktree,
    ensure_clean_worktree,
    promote,
    resolve_commit,
    validate_candidate,
)
from .manifest import EvaluatorManifest
from .models import Evidence, ResearchRequest, TaskConfig
from .process import run_or_load_once
from .results import build_public_result, public_comparison, resolve_outcome
from .sandbox import (
    command_runtime_read_paths,
    marked_command,
    sandbox_command,
    sanitized_environment,
)
from .storage import atomic_write_json, atomic_write_text, write_json_once

ExperimentState = Literal[
    "RESEARCHING",
    "CANDIDATE_FROZEN",
    "PRIMARY_RESERVED",
    "PROVISIONAL",
    "SUSPECT_RESERVED",
    "FINALIZING",
    "COMPLETE",
]
_STATES = {
    "RESEARCHING",
    "CANDIDATE_FROZEN",
    "PRIMARY_RESERVED",
    "PROVISIONAL",
    "SUSPECT_RESERVED",
    "FINALIZING",
    "COMPLETE",
}
_FIELDS = {
    "schema_version",
    "experiment_id",
    "state",
    "champion",
    "candidate",
    "public_checks_passed",
    "decision",
}


def _create_public_dossier(
    task: TaskConfig,
    experiment_directory: Path,
    public: dict[str, Any],
) -> None:
    try:
        ensure_experiment_dossier(
            experiment_directory.parent.parent,
            task,
            experiment_directory,
            public,
        )
    except (OSError, StateError):
        # A derived view must never invalidate already-published official evidence.
        pass


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: int
    state: ExperimentState
    champion: str
    candidate: str | None = None
    public_checks_passed: bool | None = None
    decision: Decision | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "state": self.state,
            "champion": self.champion,
            "candidate": self.candidate,
            "public_checks_passed": self.public_checks_passed,
            "decision": self.decision.value if self.decision is not None else None,
        }

    @classmethod
    def from_json(cls, value: Any) -> ExperimentRecord:
        if not isinstance(value, Mapping) or set(value) != _FIELDS:
            raise StateError("experiment record fields are invalid")
        identifier = value["experiment_id"]
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            raise StateError("experiment ID is invalid")
        state = value["state"]
        if state not in _STATES:
            raise StateError("experiment state is invalid")
        champion = value["champion"]
        candidate = value["candidate"]
        checks = value["public_checks_passed"]
        decision_value = value["decision"]
        if (
            value["schema_version"] != 1
            or not isinstance(champion, str)
            or not champion
            or (candidate is not None and (not isinstance(candidate, str) or not candidate))
            or (checks is not None and not isinstance(checks, bool))
            or (
                decision_value is not None
                and decision_value not in {decision.value for decision in Decision}
            )
        ):
            raise StateError("experiment record values are invalid")
        decision = Decision(decision_value) if decision_value is not None else None
        if state in ("FINALIZING", "COMPLETE") and decision in (
            None,
            Decision.PROVISIONAL,
        ):
            raise StateError("finalizing experiment must have a final decision")
        if state not in ("FINALIZING", "COMPLETE") and decision not in (
            None,
            Decision.PROVISIONAL,
        ):
            raise StateError("unfinished experiment cannot have a final decision")
        return cls(identifier, state, champion, candidate, checks, decision)


def load_experiment(directory: Path) -> ExperimentRecord:
    try:
        value = json.loads((directory / "experiment.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("experiment record is missing or invalid") from error
    return ExperimentRecord.from_json(value)


def save_experiment(directory: Path, record: ExperimentRecord) -> None:
    atomic_write_json(directory / "experiment.json", record.to_json())


def start_experiment(task_directory: Path, champion: str) -> tuple[Path, ExperimentRecord]:
    experiments = task_directory / "experiments"
    experiments.mkdir(parents=True, exist_ok=True)
    identifiers: list[int] = []
    for path in experiments.iterdir():
        if path.is_dir() and path.name.isdigit():
            identifiers.append(int(path.name))
            record = load_experiment(path)
            if record.state != "COMPLETE" or not (path / "published").exists():
                raise StateError(f"experiment {path.name} is still active")
    identifier = max(identifiers, default=0) + 1
    directory = experiments / f"{identifier:06d}"
    directory.mkdir()
    record = ExperimentRecord(
        experiment_id=identifier,
        state="RESEARCHING",
        champion=champion,
    )
    atomic_write_text(directory / "champion.commit", champion + "\n")
    save_experiment(directory, record)
    return directory, record


def load_research_request(
    path: Path,
    *,
    manifest: EvaluatorManifest,
) -> ResearchRequest:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError("research request is missing or invalid JSON") from error
    if not isinstance(value, Mapping):
        raise ValidationError("research request must contain one JSON object")
    return ResearchRequest.from_mapping(
        value,
        allowed_telemetry=manifest.public_telemetry,
    )


def freeze_candidate(
    experiment_directory: Path,
    research_worktree: Path,
    task: TaskConfig,
    manifest: EvaluatorManifest,
) -> tuple[ExperimentRecord, ResearchRequest]:
    record = load_experiment(experiment_directory)
    request = load_research_request(
        experiment_directory / "request.public.json",
        manifest=manifest,
    )
    if record.state not in ("RESEARCHING", "CANDIDATE_FROZEN"):
        raise StateError("candidate cannot be frozen in the current experiment state")

    candidate_path = experiment_directory / "candidate.commit"
    if candidate_path.exists():
        candidate = candidate_path.read_text(encoding="utf-8").strip()
        validate_candidate(
            task.repo,
            champion=record.champion,
            candidate=candidate,
            editable_paths=task.editable_paths,
            denied_paths=task.denied_paths,
        )
    else:
        candidate, _ = create_candidate_commit(
            research_worktree,
            champion=record.champion,
            editable_paths=task.editable_paths,
            denied_paths=task.denied_paths,
            prior_candidate_ref_prefix=f"refs/arctl/{task.task_id}/candidates/",
            message=f"arctl experiment {record.experiment_id}",
        )
        atomic_write_text(candidate_path, candidate + "\n")

    candidate_ref = (
        f"refs/arctl/{task.task_id}/candidates/{record.experiment_id:06d}"
    )
    create_candidate_ref(task.repo, candidate_ref, candidate)
    updated = replace(record, state="CANDIDATE_FROZEN", candidate=candidate)
    save_experiment(experiment_directory, updated)
    return updated, request


def run_public_checks(
    task_directory: Path,
    experiment_directory: Path,
    task: TaskConfig,
    *,
    command_builder: Callable[[Sequence[str], Path, Path], Sequence[str]] | None = None,
    stop_path: Path | None = None,
) -> bool:
    record = load_experiment(experiment_directory)
    if record.state != "CANDIDATE_FROZEN" or record.candidate is None:
        raise StateError("public checks require a frozen candidate")
    worktree = task_directory / "worktrees" / f"{record.experiment_id:06d}-candidate"
    if not worktree.exists():
        create_detached_worktree(task.repo, worktree, record.candidate)
    elif resolve_commit(worktree, "HEAD") != record.candidate:
        raise StateError("candidate check worktree points at the wrong commit")
    ensure_clean_worktree(worktree)

    failure = experiment_directory / "public-check.failure.json"
    if failure.is_file():
        for path in (
            experiment_directory / "process",
            experiment_directory / "outputs",
        ):
            for stale in path.glob("public-check-*"):
                shutil.rmtree(stale)
        failure.unlink()

    passed = True
    for index, command in enumerate(task.public_checks, start=1):
        output = experiment_directory / "outputs" / f"public-check-{index:04d}"
        output.mkdir(parents=True, exist_ok=True)
        if command_builder is None:
            execution_marker = output / "execution.started"
            codex_home = output / "codex-home"
            writable_home = output / "home"
            codex_home.mkdir()
            writable_home.mkdir()
            managed_command = sandbox_command(
                marked_command(command, execution_marker),
                cwd=worktree,
                read_paths=(worktree, *command_runtime_read_paths(command)),
                write_paths=(output,),
                profile="arctl-subject",
            )
            environment = sanitized_environment(
                codex_home=codex_home,
                writable_home=writable_home,
            )
        else:
            execution_marker = None
            managed_command = command_builder(command, worktree, output)
            environment = None
        try:
            result = run_or_load_once(
                experiment_directory / "process" / f"public-check-{index:04d}",
                managed_command,
                timeout_seconds=600,
                max_output_bytes=1_000_000,
                cwd=worktree,
                env=environment,
                stop_path=stop_path,
            )
        except StoppedError:
            raise
        except (ProcessError, StateError) as error:
            if execution_marker is not None and not execution_marker.is_file():
                write_json_once(
                    failure,
                    {
                        "schema_version": 1,
                        "check": index,
                        "message": "public-check sandbox did not start its command",
                    },
                )
                raise StateError(
                    "public-check sandbox did not start its command"
                ) from error
            passed = False
            break
        if result["return_code"] != 0:
            if execution_marker is not None and not execution_marker.is_file():
                write_json_once(
                    failure,
                    {
                        "schema_version": 1,
                        "check": index,
                        "message": "public-check sandbox did not start its command",
                    },
                )
                raise StateError("public-check sandbox did not start its command")
            passed = False
            break
        try:
            ensure_clean_worktree(worktree)
        except StateError:
            passed = False
            break
    updated = replace(record, public_checks_passed=passed)
    save_experiment(experiment_directory, updated)
    return passed


def mark_comparison_reserved(
    experiment_directory: Path,
    *,
    kind: Literal["primary", "suspect"],
) -> ExperimentRecord:
    record = load_experiment(experiment_directory)
    expected = "CANDIDATE_FROZEN" if kind == "primary" else "PROVISIONAL"
    target: ExperimentState = "PRIMARY_RESERVED" if kind == "primary" else "SUSPECT_RESERVED"
    if record.state != expected:
        raise StateError(f"{kind} comparison cannot be reserved from {record.state}")
    if kind == "primary" and record.public_checks_passed is not True:
        raise StateError("primary comparison requires passing public checks")
    updated = replace(record, state=target)
    save_experiment(experiment_directory, updated)
    return updated


def save_comparison_result(
    experiment_directory: Path,
    primary: Evidence,
    suspect: Evidence | None = None,
) -> ExperimentRecord:
    record = load_experiment(experiment_directory)
    outcome = resolve_outcome(primary, suspect)
    if not outcome.final:
        if record.state != "PRIMARY_RESERVED":
            raise StateError("provisional result requires a reserved primary comparison")
        updated = replace(record, state="PROVISIONAL", decision=Decision.PROVISIONAL)
    else:
        expected = "SUSPECT_RESERVED" if suspect is not None else "PRIMARY_RESERVED"
        if record.state != expected:
            raise StateError("final result does not match the experiment state")
        updated = replace(record, state="FINALIZING", decision=outcome.decision)
    save_experiment(experiment_directory, updated)
    return updated


def publish_final_result(
    task: TaskConfig,
    experiment_directory: Path,
    manifest: EvaluatorManifest,
    request: ResearchRequest,
    primary: Evidence,
    suspect: Evidence | None = None,
) -> dict[str, Any]:
    record = load_experiment(experiment_directory)
    if record.candidate is None or record.public_checks_passed is not True:
        raise StateError("final publication requires a checked frozen candidate")
    outcome = resolve_outcome(primary, suspect)
    if not outcome.final:
        raise StateError("provisional outcome cannot be published")
    if record.state not in ("FINALIZING", "COMPLETE") or record.decision != outcome.decision:
        raise StateError("experiment record does not contain this final outcome")

    if outcome.may_promote:
        promote(
            task.repo,
            champion_ref=f"refs/arctl/{task.task_id}/champion",
            candidate=record.candidate,
            expected_champion=record.champion,
        )
    elif resolve_commit(
        task.repo, f"refs/arctl/{task.task_id}/champion"
    ) != record.champion:
        raise StateError("champion changed before final publication")
    public = build_public_result(
        experiment_id=record.experiment_id,
        hypothesis=request.claim,
        champion_before=record.champion,
        candidate=record.candidate,
        statistic=manifest.public_statistic,
        outcome=outcome,
        tests_pass=True,
    )
    write_json_once(experiment_directory / "result.public.json", public)
    if record.state != "COMPLETE":
        save_experiment(experiment_directory, replace(record, state="COMPLETE"))
    atomic_write_text(experiment_directory / "published", "")
    _create_public_dossier(task, experiment_directory, public)
    return public


def publish_candidate_rejection(
    task: TaskConfig,
    experiment_directory: Path,
    request: ResearchRequest,
) -> dict[str, Any]:
    record = load_experiment(experiment_directory)
    if record.state != "CANDIDATE_FROZEN" or record.candidate is None:
        raise StateError("candidate rejection requires a frozen candidate")
    if record.public_checks_passed is not False:
        raise StateError("candidate rejection requires failed public checks")
    if resolve_commit(
        task.repo, f"refs/arctl/{task.task_id}/champion"
    ) != record.champion:
        raise StateError("champion changed before candidate rejection")
    public = {
        "experiment_id": record.experiment_id,
        "hypothesis": request.claim,
        "champion_before": record.champion,
        "candidate": record.candidate,
        "champion_after": record.champion,
        "decision": Decision.REJECT.value,
        "evaluation": {"statistic": None, "comparisons": []},
        "constraints": {"tests": "FAIL"},
        "telemetry": {},
    }
    write_json_once(experiment_directory / "result.public.json", public)
    updated = replace(record, state="COMPLETE", decision=Decision.REJECT)
    save_experiment(experiment_directory, updated)
    atomic_write_text(experiment_directory / "published", "")
    _create_public_dossier(task, experiment_directory, public)
    return public


def publish_comparison_failure(
    task: TaskConfig,
    experiment_directory: Path,
    request: ResearchRequest,
    manifest: EvaluatorManifest,
    *,
    source: Literal[
        "candidate",
        "champion",
        "evaluator",
        "evidence",
        "stop",
        "sandbox",
    ],
    primary: Evidence | None = None,
) -> dict[str, Any]:
    record = load_experiment(experiment_directory)
    if record.state not in ("PRIMARY_RESERVED", "PROVISIONAL", "SUSPECT_RESERVED"):
        raise StateError("comparison failure requires a reserved comparison")
    if record.state in ("PROVISIONAL", "SUSPECT_RESERVED") and primary is None:
        raise StateError("post-primary failure must retain the valid primary evidence")
    if record.state == "PROVISIONAL" and source != "stop":
        raise StateError("only a stop may terminate an unreserved provisional result")
    if (
        record.state == "PRIMARY_RESERVED"
        and primary is not None
        and source != "stop"
    ):
        raise StateError("primary failure cannot attach uncommitted primary evidence")
    if record.candidate is None or record.public_checks_passed is not True:
        raise StateError("comparison failure requires a checked frozen candidate")
    if resolve_commit(
        task.repo, f"refs/arctl/{task.task_id}/champion"
    ) != record.champion:
        raise StateError("champion changed before failure publication")

    decision = failure_decision(source)
    public = {
        "experiment_id": record.experiment_id,
        "hypothesis": request.claim,
        "champion_before": record.champion,
        "candidate": record.candidate,
        "champion_after": record.champion,
        "decision": decision.value,
        "evaluation": {
            "statistic": manifest.public_statistic,
            "comparisons": (
                [public_comparison(primary)] if primary is not None else []
            ),
        },
        "constraints": {"tests": "PASS"},
        "telemetry": dict(primary.telemetry) if primary is not None else {},
        "failure": (
            "candidate_execution" if source == "candidate" else "system_execution"
        ),
    }
    write_json_once(experiment_directory / "result.public.json", public)
    save_experiment(
        experiment_directory,
        replace(record, state="COMPLETE", decision=decision),
    )
    atomic_write_text(experiment_directory / "published", "")
    _create_public_dossier(task, experiment_directory, public)
    return public
