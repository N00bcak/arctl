"""Public, advisory interpretation of final statistical evidence."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

from .downstream import transient_process_error
from .errors import ProcessError, StateError, StoppedError, TransientDownstreamError
from .manifest import EvaluatorManifest
from .models import TaskConfig
from .process import run_or_load_once
from .sandbox import research_command, sanitized_environment
from .search import load_ledger
from .storage import write_json_once

ReflectionCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]


def reflection_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}

    def strict(properties: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": dict(properties),
        }

    return strict(
        {
            "schema_version": {"type": "integer", "const": 1},
            "summary": text,
            "strategy_behavior": strict(
                {
                    "id": text,
                    "realization": {
                        "type": "string",
                        "enum": ["expressed", "not_expressed", "unclear"],
                    },
                    "evidence": {"type": "array", "items": text},
                }
            ),
            "metric_assessments": {
                "type": "array",
                "items": strict(
                    {
                        "metric": text,
                        "finding": {
                            "type": "string",
                            "enum": ["supports", "contradicts", "inconclusive"],
                        },
                        "rationale": text,
                    }
                ),
            },
            "mechanism": strict(
                {
                    "status": {
                        "type": "string",
                        "enum": ["supported", "contradicted", "not_demonstrated"],
                    },
                    "evidence": {"type": "array", "items": text},
                    "missing_evidence": {"type": "array", "items": text},
                }
            ),
            "implementation": strict(
                {
                    "status": {
                        "type": "string",
                        "enum": [
                            "no_specific_concern",
                            "activation_unclear",
                            "test_gap",
                            "implementation_concern",
                        ],
                    },
                    "evidence": {"type": "array", "items": text},
                    "concerns": {"type": "array", "items": text},
                }
            ),
            "policy_observations": {
                "type": "array",
                "items": strict(
                    {
                        "finding": text,
                        "evidence": text,
                        "implication": text,
                    }
                ),
            },
            "next_action": strict(
                {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "retain",
                            "refine",
                            "revisit_after_better_evidence",
                            "audit_implementation",
                            "abandon_direction",
                        ],
                    },
                    "rationale": text,
                    "test": text,
                }
            ),
        }
    )


def _basis(
    task: TaskConfig,
    manifest: EvaluatorManifest,
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    comparisons = result["evaluation"]["comparisons"]
    final = comparisons[-1]
    return {
        "objective": task.objective,
        "experiment_id": result["experiment_id"],
        "verdict": result["decision"],
        "statistic": result["evaluation"]["statistic"],
        "comparisons": comparisons,
        "effect_estimate": final["effect_estimate"],
        "one_sided_lower_bound": final["one_sided_lower_bound"],
        "uncertainty_margin": (
            final["effect_estimate"] - final["one_sided_lower_bound"]
        ),
        "trial_count": final["trials"],
        "statistical_method": manifest.uncertainty_method,
        "known_variation": manifest.known_variation,
        "variation_mitigations": list(manifest.variation_mitigations),
        "claim": request["claim"],
        "strategy_behavior_id": request["strategy_behavior_id"],
        "mechanism": request["mechanism"],
        "viability": request["viability"],
        "evidence_review": request["evidence_review"],
        "lineage": request["lineage"],
        "expected_effect": request["expected_effect"],
        "expected_telemetry": request["expected_telemetry"],
        "falsifiers": request["falsifiers"],
        "telemetry_contract": {
            name: asdict(metric) for name, metric in manifest.public_telemetry.items()
        },
        "telemetry": result["telemetry"],
        "constraints": result["constraints"],
        "public_checks": [list(command) for command in task.public_checks],
    }


def _research_context(experiment: Path) -> dict[str, Any]:
    task_directory = experiment.parent.parent
    strategy_files = sorted((task_directory / "strategy").glob("*.public.json"))
    strategy = None
    strategy_name = None
    if strategy_files:
        try:
            strategy = json.loads(strategy_files[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("saved strategy is invalid") from error
        strategy_name = strategy_files[-1].name
    ledger = load_ledger(task_directory)
    return {
        "strategy": strategy,
        "strategy_source": strategy_name,
        "exploration": ledger,
    }


def _next_attempt(root: Path) -> Path:
    attempts = [int(path.name) for path in root.glob("[0-9][0-9][0-9][0-9]")]
    return root / f"{max(attempts, default=0) + 1:04d}"


def validate_reflection(
    value: Any,
    *,
    metric_names: Sequence[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "status",
        "warning",
        "basis",
        "assessment",
    }:
        raise StateError("saved reflection fields are invalid")
    if value["schema_version"] != 1 or not isinstance(value["basis"], Mapping):
        raise StateError("saved reflection values are invalid")
    if value["status"] == "SKIPPED_NO_TELEMETRY":
        if (
            metric_names
            or not isinstance(value["warning"], str)
            or value["assessment"] is not None
        ):
            raise StateError("saved skipped reflection is invalid")
    elif value["status"] == "COMPLETE":
        if value["warning"] is not None:
            raise StateError("saved complete reflection is invalid")
        try:
            Draft202012Validator(reflection_schema()).validate(value["assessment"])
        except JsonSchemaError as error:
            raise StateError("saved reflection assessment is invalid") from error
        names = [item["metric"] for item in value["assessment"]["metric_assessments"]]
        if len(names) != len(set(names)) or set(names) != set(metric_names):
            raise StateError("reflection must assess every telemetry metric exactly once")
        if (
            value["assessment"]["strategy_behavior"]["id"]
            != value["basis"]["strategy_behavior_id"]
        ):
            raise StateError("reflection strategy behavior does not match the request")
    else:
        raise StateError("saved reflection status is invalid")
    return dict(value)


def run_reflection(
    *,
    task: TaskConfig,
    experiment: Path,
    manifest: EvaluatorManifest,
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    candidate_worktree: Path,
    champion_worktree: Path,
    stop_path: Path,
    command_builder: ReflectionCommandBuilder | None = None,
) -> dict[str, Any]:
    basis = _basis(task, manifest, request, result)
    context = _research_context(experiment)
    basis["context_refs"] = {
        "strategy": context["strategy_source"],
        "ledger_entries": [entry.get("entry_id") for entry in context["exploration"]],
    }
    published = experiment / "reflection.public.json"
    if published.is_file():
        try:
            value = json.loads(published.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("saved reflection is invalid") from error
        validated = validate_reflection(
            value, metric_names=tuple(manifest.public_telemetry)
        )
        if validated["basis"] != basis:
            raise StateError("saved reflection basis changed")
        return validated

    attempt = _next_attempt(experiment / "reflection" / "attempts")
    scratch = attempt / "output"
    scratch.mkdir(parents=True, exist_ok=True)
    if not manifest.public_telemetry:
        value = {
            "schema_version": 1,
            "status": "SKIPPED_NO_TELEMETRY",
            "warning": (
                "The approved evaluator publishes no telemetry; causal reflection "
                "was skipped."
            ),
            "basis": basis,
            "assessment": None,
        }
        write_json_once(published, value)
        return value

    schema = reflection_schema()
    schema_path = scratch / "reflection.schema.json"
    write_json_once(schema_path, schema)
    prompt = (
        "Reflect on this completed experiment without changing its statistical verdict. "
        "Inspect the candidate and champion implementation when useful. Separate direct "
        "observations from inference, do not invent causes that the aggregate evidence "
        "cannot identify, and treat implementation incompetence as a hypothesis requiring "
        "specific evidence. Assess whether the candidate actually expressed the selected "
        "strategic behavior, and record policy-specific observations about the proposed "
        "mechanism or implementation for later executors. Assess every declared telemetry "
        "metric exactly once. Return "
        "only the required reflection JSON. The candidate is the current working "
        f"directory; the champion is readable at {champion_worktree}.\n\n"
        + json.dumps(
            {"experiment": basis, "research_context": context},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if command_builder is None:
        command = research_command(
            worktree=candidate_worktree,
            scratch=scratch,
            output_schema=schema_path,
            prompt=prompt,
            read_paths=(champion_worktree,),
            output_name="assessment.public.json",
            model=task.strategy_model,
            reasoning_effort=task.strategy_reasoning_effort,
            writable_worktree=False,
        )
        environment = sanitized_environment(
            codex_home=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
            writable_home=scratch,
        )
    else:
        command = command_builder(candidate_worktree, scratch, schema_path, prompt)
        environment = None
    try:
        process = run_or_load_once(
            attempt / "process",
            command,
            timeout_seconds=3600,
            max_output_bytes=2_000_000,
            cwd=candidate_worktree,
            env=environment,
            stop_path=stop_path,
        )
        if process["return_code"] != 0:
            transient = transient_process_error(
                attempt / "process",
                stage="reflection",
                codex=command_builder is None,
            )
            if transient is not None:
                raise transient
            raise StateError("fresh reflection session exited unsuccessfully")
        assessment = json.loads(
            (scratch / "assessment.public.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(assessment)
        names = [item["metric"] for item in assessment["metric_assessments"]]
        if len(names) != len(set(names)) or set(names) != set(manifest.public_telemetry):
            raise StateError("reflection must assess every telemetry metric exactly once")
        if assessment["strategy_behavior"]["id"] != request["strategy_behavior_id"]:
            raise StateError("reflection strategy behavior does not match the request")
    except StoppedError:
        raise
    except TransientDownstreamError as error:
        write_json_once(
            attempt / "reflection.failure.json",
            {"schema_version": 1, "message": str(error)},
        )
        raise
    except ProcessError as error:
        transient = transient_process_error(
            attempt / "process",
            stage="reflection",
            codex=command_builder is None,
            fallback=str(error),
        )
        if transient is not None:
            write_json_once(
                attempt / "reflection.failure.json",
                {"schema_version": 1, "message": str(transient)},
            )
            raise transient from error
        write_json_once(
            attempt / "reflection.failure.json",
            {"schema_version": 1, "message": str(error)},
        )
        raise StateError("post-trial reflection failed") from error
    except (OSError, json.JSONDecodeError, JsonSchemaError, StateError) as error:
        write_json_once(
            attempt / "reflection.failure.json",
            {"schema_version": 1, "message": str(error)},
        )
        raise StateError("post-trial reflection failed") from error
    value = {
        "schema_version": 1,
        "status": "COMPLETE",
        "warning": None,
        "basis": basis,
        "assessment": assessment,
    }
    write_json_once(published, value)
    return value
