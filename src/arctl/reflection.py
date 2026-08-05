"""Public, advisory interpretation of final statistical evidence."""

from __future__ import annotations

import hashlib
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
from .sandbox import agent_prompt_path, research_command, sanitized_environment
from .search import load_ledger
from .storage import write_json_once

ReflectionCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]


def reflection_schema(*, version: int = 2) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}

    def strict(properties: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": dict(properties),
        }

    properties = {
            "schema_version": {"type": "integer", "const": version},
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
    if version == 2:
        properties["history_citations"] = {
            "type": "array",
            "items": strict(
                {
                    "entry_id": text,
                    "bearing": {
                        "type": "string",
                        "enum": ["supports", "contradicts", "unresolved"],
                    },
                    "finding": text,
                }
            ),
        }
    return strict(properties)


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
    strategy_name = None
    if strategy_files:
        strategy_name = strategy_files[-1].name
    supporting: dict[str, str] = {}
    for name in ("planning.public.json", "implementation.public.json"):
        path = experiment / name
        if path.is_file():
            supporting[name] = str(path)
    catalog = task_directory / "exploration" / "ledger.public.jsonl"
    catalog_bytes = catalog.read_bytes() if catalog.is_file() else b""
    return {
        "strategy_source": strategy_name,
        "strategy": str(strategy_files[-1]) if strategy_files else None,
        "exploration_catalog": str(catalog),
        "exploration_entries": str(task_directory / "exploration" / "entries"),
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "supporting_artifacts": supporting,
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
    if value["schema_version"] not in {1, 2} or not isinstance(value["basis"], Mapping):
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
            assessment_version = value["assessment"].get("schema_version")
            if assessment_version not in {1, 2}:
                raise StateError("saved reflection assessment version is invalid")
            Draft202012Validator(
                reflection_schema(version=assessment_version)
            ).validate(value["assessment"])
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
        "catalog_sha256": context["catalog_sha256"],
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

    schema = reflection_schema(version=2)
    schema_path = scratch / "reflection.schema.json"
    write_json_once(schema_path, schema)
    prompt = (
        "Reflect on this completed experiment without changing its statistical verdict. "
        "Inspect the candidate and champion implementation when useful. Separate direct "
        "observations from inference, do not invent causes that the aggregate evidence "
        "cannot identify, and treat implementation incompetence as a hypothesis requiring "
        "specific evidence. Separately assess whether the candidate actually expressed "
        "the selected behavior, whether its mechanism activated, whether implementation "
        "fidelity was compromised, and whether the mechanism is supported, contradicted, "
        "or inconclusive. Use strategy_behavior for realization, mechanism evidence and "
        "missing_evidence for activation, and implementation for fidelity. "
        "Record policy-specific observations about the proposed mechanism or "
        "implementation for later planners. Search the compact public "
        "catalog, open only relevant canonical entries, and cite every history entry "
        "relied upon. Recommend only a disposition; "
        "do not invent the next candidate. Assess every declared telemetry "
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
        history_paths = [experiment.parent.parent / "exploration"]
        if context["strategy"] is not None:
            history_paths.append(Path(context["strategy"]))
        history_paths.extend(
            Path(path) for path in context["supporting_artifacts"].values()
        )
        command = research_command(
            worktree=candidate_worktree,
            scratch=scratch,
            output_schema=schema_path,
            prompt=prompt,
            read_paths=(champion_worktree, *history_paths),
            output_name="assessment.public.json",
            model=task.reflection_model,
            reasoning_effort=task.reflection_reasoning_effort,
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
            stdin_path=(
                agent_prompt_path(scratch) if command_builder is None else None
            ),
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
        assessment_version = assessment.get("schema_version")
        if assessment_version not in {1, 2}:
            raise StateError("reflection schema version is unsupported")
        Draft202012Validator(
            reflection_schema(version=assessment_version)
        ).validate(assessment)
        names = [item["metric"] for item in assessment["metric_assessments"]]
        if len(names) != len(set(names)) or set(names) != set(manifest.public_telemetry):
            raise StateError("reflection must assess every telemetry metric exactly once")
        if assessment["strategy_behavior"]["id"] != request["strategy_behavior_id"]:
            raise StateError("reflection strategy behavior does not match the request")
        if assessment_version == 2:
            citation_ids = [item["entry_id"] for item in assessment["history_citations"]]
            known_ids = {
                entry["entry_id"] for entry in load_ledger(experiment.parent.parent)
            }
            if any(identifier not in known_ids for identifier in citation_ids):
                raise StateError("reflection cites invalid exploration entries")
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
        raise StateError(f"post-trial reflection failed: {error}") from error
    except (OSError, json.JSONDecodeError, JsonSchemaError, StateError) as error:
        write_json_once(
            attempt / "reflection.failure.json",
            {"schema_version": 1, "message": str(error)},
        )
        raise StateError(f"post-trial reflection failed: {error}") from error
    value = {
        "schema_version": 2,
        "status": "COMPLETE",
        "warning": None,
        "basis": basis,
        "assessment": assessment,
    }
    write_json_once(published, value)
    return value
