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

from .agent_backend import AgentSessionRequest, agent_command, agent_environment, agent_provenance
from .agent_selection import select_agent
from .downstream import transient_process_error
from .errors import ArctlError, ProcessError, StateError, StoppedError, TransientDownstreamError
from .manifest import EvaluatorManifest
from .models import TaskConfig
from .process import run_or_load_once
from .sandbox import agent_prompt_path
from .search import load_ledger
from .storage import write_json_once

ReflectionCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]


def reflection_schema(
    *,
    version: int = 2,
    metric_names: Sequence[str] | None = None,
    strategy_behavior_id: str | None = None,
    history_entry_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    if version in {3, 4} and metric_names is None:
        raise ValueError(f"reflection schema version {version} requires metric_names")
    text = {"type": "string", "minLength": 1}

    def strict(
        properties: Mapping[str, Any], *, required: Sequence[str] | None = None
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties) if required is None else list(required),
            "properties": dict(properties),
        }

    if version == 4:
        history_id_schema: dict[str, Any]
        if history_entry_ids:
            history_id_schema = {"type": "string", "enum": list(history_entry_ids)}
        else:
            history_id_schema = text
        properties = {
            "schema_version": {"type": "integer", "const": 4},
            "summary": text,
            "strategy_behavior": strict(
                {
                    "id": (
                        {"type": "string", "const": strategy_behavior_id}
                        if strategy_behavior_id is not None
                        else text
                    ),
                    "realization": {
                        "type": "string",
                        "enum": ["expressed", "not_expressed", "unclear"],
                    },
                    "evidence": {"type": "array", "items": text, "maxItems": 2},
                }
            ),
            "mechanism": strict(
                {
                    "status": {
                        "type": "string",
                        "enum": ["supported", "contradicted", "not_demonstrated"],
                    },
                    "evidence": {"type": "array", "items": text, "maxItems": 2},
                    "missing_evidence": {
                        "type": "array",
                        "items": text,
                        "maxItems": 2,
                    },
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
                    "concerns": {"type": "array", "items": text, "maxItems": 2},
                }
            ),
            "material_signals": {
                "type": "array",
                "maxItems": 5,
                "items": strict(
                    {
                        "metric": {"type": "string", "enum": list(metric_names)},
                        "finding": {
                            "type": "string",
                            "enum": [
                                "supports",
                                "contradicts",
                                "inconclusive",
                                "anomalous",
                            ],
                        },
                        "interpretation": text,
                    }
                ),
            },
            "history_citations": {
                "type": "array",
                "maxItems": 3,
                "items": strict(
                    {
                        "entry_id": history_id_schema,
                        "bearing": {
                            "type": "string",
                            "enum": ["supports", "contradicts", "unresolved"],
                        },
                        "finding": text,
                    }
                ),
            },
            "policy_observations": {
                "type": "array",
                "maxItems": 3,
                "items": strict(
                    {"finding": text, "evidence": text, "implication": text}
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
                },
                required=("kind", "rationale"),
            ),
        }
        schema = strict(properties)
        if history_entry_ids is not None and not history_entry_ids:
            schema["properties"]["history_citations"]["maxItems"] = 0
        return schema

    properties = {
            "schema_version": {"type": "integer", "const": version},
            "summary": text,
            "strategy_behavior": strict(
                {
                    "id": (
                        {"type": "string", "const": strategy_behavior_id}
                        if strategy_behavior_id is not None
                        else text
                    ),
                    "realization": {
                        "type": "string",
                        "enum": ["expressed", "not_expressed", "unclear"],
                    },
                    "evidence": {"type": "array", "items": text},
                }
            ),
            "metric_assessments": (
                strict(
                    {
                        name: strict(
                            {
                                "finding": {
                                    "type": "string",
                                    "enum": ["supports", "contradicts", "inconclusive"],
                                },
                                "rationale": text,
                            }
                        )
                        for name in metric_names
                    }
                )
                if version == 3 and metric_names is not None
                else {
                    "type": "object",
                    "additionalProperties": strict(
                        {
                            "finding": {
                                "type": "string",
                                "enum": ["supports", "contradicts", "inconclusive"],
                            },
                            "rationale": text,
                        }
                    ),
                }
                if version == 3
                else {
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
                }
            ),
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
    if version in {2, 3}:
        properties["history_citations"] = {
            "type": "array",
            "items": strict(
                {
                    "entry_id": (
                        {"type": "string", "enum": list(history_entry_ids)}
                        if version == 3 and history_entry_ids
                        else text
                    ),
                    "bearing": {
                        "type": "string",
                        "enum": ["supports", "contradicts", "unresolved"],
                    },
                    "finding": text,
                }
            ),
        }
        if version == 3 and history_entry_ids is not None and not history_entry_ids:
            properties["history_citations"]["maxItems"] = 0
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
    paths = sorted(root.glob("[0-9][0-9][0-9][0-9]"))
    attempts = [int(path.name) for path in paths]
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
    if value["schema_version"] not in {1, 2, 3, 4} or not isinstance(
        value["basis"], Mapping
    ):
        raise StateError("saved reflection values are invalid")
    if value["status"] == "SKIPPED_NO_TELEMETRY":
        if (
            metric_names
            or not isinstance(value["warning"], str)
            or value["assessment"] is not None
        ):
            raise StateError("saved skipped reflection is invalid")
    elif value["status"] == "COMPLETE":
        assessment = value["assessment"]
        if value["warning"] is not None or not isinstance(assessment, Mapping):
            raise StateError("saved complete reflection is invalid")
        try:
            assessment_version = assessment.get("schema_version")
            if assessment_version not in {1, 2, 3, 4}:
                raise StateError("saved reflection assessment version is invalid")
            Draft202012Validator(
                reflection_schema(
                    version=assessment_version,
                    metric_names=(
                        metric_names if assessment_version in {3, 4} else None
                    ),
                    strategy_behavior_id=(
                        value["basis"].get("strategy_behavior_id")
                        if assessment_version in {3, 4}
                        else None
                    ),
                )
            ).validate(assessment)
        except JsonSchemaError as error:
            raise StateError("saved reflection assessment is invalid") from error
        if assessment_version == 4:
            names = [item["metric"] for item in assessment["material_signals"]]
            if len(names) != len(set(names)) or any(
                name not in metric_names for name in names
            ):
                raise StateError(
                    "reflection material signals must be unique declared metrics"
                )
            citation_ids = [
                item["entry_id"] for item in assessment["history_citations"]
            ]
            if len(citation_ids) != len(set(citation_ids)):
                raise StateError("reflection history citations must be unique")
            raw_history_ids = value["basis"].get("history_entry_ids", [])
            if (
                not isinstance(raw_history_ids, list)
                or not all(isinstance(identifier, str) for identifier in raw_history_ids)
                or len(raw_history_ids) != len(set(raw_history_ids))
            ):
                raise StateError("saved reflection history basis is invalid")
            known_history_ids = set(raw_history_ids)
            if any(identifier not in known_history_ids for identifier in citation_ids):
                raise StateError("reflection cites unknown history entries")
        elif assessment_version == 3:
            assessments = assessment["metric_assessments"]
            if set(assessments) != set(metric_names):
                raise StateError(
                    "reflection must assess every telemetry metric exactly once"
                )
        else:
            assessments = assessment["metric_assessments"]
            names = [item["metric"] for item in assessments]
            if len(names) != len(set(names)) or set(names) != set(metric_names):
                raise StateError(
                    "reflection must assess every telemetry metric exactly once"
                )
        if (
            assessment["strategy_behavior"]["id"]
            != value["basis"].get("strategy_behavior_id")
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
    python_cache: Path | None = None,
) -> dict[str, Any]:
    assert task.method is not None
    task.method.require_component("reflect", "reflect.evidence-v1")
    basis = _basis(task, manifest, request, result)
    context = _research_context(experiment)
    basis["context_refs"] = {
        "strategy": context["strategy_source"],
        "catalog_sha256": context["catalog_sha256"],
    }
    history_ids = tuple(
        entry["entry_id"] for entry in load_ledger(experiment.parent.parent)
    )
    published = experiment / "reflection.public.json"
    if published.is_file():
        try:
            value = json.loads(published.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("saved reflection is invalid") from error
        saved_basis = value.get("basis") if isinstance(value, Mapping) else None
        if isinstance(saved_basis, Mapping) and "history_entry_ids" in saved_basis:
            basis["history_entry_ids"] = list(history_ids)
        validated = validate_reflection(
            value, metric_names=tuple(manifest.public_telemetry)
        )
        if validated["basis"] != basis:
            raise StateError("saved reflection basis changed")
        return validated

    reflection_root = experiment / "reflection"
    attempt = _next_attempt(reflection_root / "attempts")
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

    basis["history_entry_ids"] = list(history_ids)
    schema = reflection_schema(
        version=4,
        metric_names=tuple(manifest.public_telemetry),
        strategy_behavior_id=request["strategy_behavior_id"],
        history_entry_ids=history_ids,
    )
    schema_path = scratch / "reflection.schema.json"
    write_json_once(schema_path, schema)
    prompt = (
        "Reflect on this completed experiment without changing its statistical verdict. "
        "Be concise and do not restate claims, verdicts, numerical results, unchanged "
        "telemetry, implementation summaries, or history already present in the basis. "
        "Inspect the candidate and champion only when useful. Separate observation from "
        "inference and do not infer causes that aggregate evidence cannot identify. "
        "Assess behavior realization, mechanism activation, and implementation fidelity. "
        "Include telemetry only when its interpretation changes the conclusion or exposes "
        "an anomaly; material_signals may be empty. Record implementation evidence only "
        "for a concrete concern or a non-obvious fidelity conclusion. State shared "
        "limitations such as unmeasured activation once. Record only novel, reusable "
        "policy observations. Search the compact public catalog, open only relevant "
        "canonical entries, and cite only history actually relied upon. Recommend a "
        "disposition and optional specific test, not a new candidate design. Return "
        "only the required reflection JSON. The candidate is the current working "
        f"directory; the champion is readable at {champion_worktree}.\n\n"
        + json.dumps(
            {"experiment": basis, "research_context": context},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    try:
        if command_builder is None:
            history_paths = [experiment.parent.parent / "exploration"]
            if context["strategy"] is not None:
                history_paths.append(Path(context["strategy"]))
            history_paths.extend(
                Path(path) for path in context["supporting_artifacts"].values()
            )
            assert task.method is not None
            agent = select_agent(
                task.method,
                component="reflect",
                lifecycle=f"reflection:{experiment.name}",
                root=reflection_root,
            )
            read_paths = (champion_worktree, *history_paths)
            command = agent_command(
                agent,
                AgentSessionRequest(
                    worktree=candidate_worktree,
                    scratch=scratch,
                    output_schema=schema_path,
                    prompt=prompt,
                    read_paths=read_paths,
                    write_paths=((python_cache,) if python_cache is not None else ()),
                    output_name="assessment.public.json",
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
            if python_cache is not None:
                environment.pop("PYTHONDONTWRITEBYTECODE", None)
                environment["PYTHONPYCACHEPREFIX"] = str(python_cache.resolve())
            write_json_once(
                attempt / "agent.public.json",
                agent_provenance(agent, lifecycle=f"reflection:{experiment.name}"),
            )
        else:
            command = command_builder(candidate_worktree, scratch, schema_path, prompt)
            environment = None
    except StoppedError:
        raise
    except (OSError, ArctlError) as error:
        write_json_once(
            attempt / "reflection.failure.json",
            {"schema_version": 1, "message": str(error)},
        )
        raise StateError(f"post-trial reflection failed: {error}") from error
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
        if assessment_version != 4:
            raise StateError("new reflections must use schema version 4")
        Draft202012Validator(schema).validate(assessment)
        names = [item["metric"] for item in assessment["material_signals"]]
        if len(names) != len(set(names)):
            raise StateError("reflection material signals must be unique")
        if assessment["strategy_behavior"]["id"] != request["strategy_behavior_id"]:
            raise StateError("reflection strategy behavior does not match the request")
        citation_ids = [item["entry_id"] for item in assessment["history_citations"]]
        if len(citation_ids) != len(set(citation_ids)):
            raise StateError("reflection history citations must be unique")
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
        "schema_version": 4,
        "status": "COMPLETE",
        "warning": None,
        "basis": basis,
        "assessment": assessment,
    }
    write_json_once(published, value)
    return value
