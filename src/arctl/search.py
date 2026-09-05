"""Public strategy, comparative planning, and exploration records."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

from .agent_backend import AgentSessionRequest, agent_command, agent_environment, agent_provenance
from .agent_selection import select_agent
from .downstream import transient_process_error
from .errors import (
    ProcessError,
    ResearchMiss,
    StateError,
    StoppedError,
    TransientDownstreamError,
    ValidationError,
)
from .manifest import EvaluatorManifest
from .models import ResearchRequest
from .process import run_or_load_once
from .registry import LocatedTask
from .results import canonical_result
from .sandbox import (
    agent_prompt_path,
    command_runtime_read_paths,
)
from .storage import atomic_write_text, write_json_once

AgentCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]


def _strict_schema(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": dict(properties),
    }


def strategy_schema(*, source_ids: Sequence[str] | None = None) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    evidence = _strict_schema(
        {
            "source_id": (
                {"type": "string", "enum": list(source_ids)}
                if source_ids is not None
                else text
            ),
            "location": text,
            "finding": text,
        }
    )
    observation = _strict_schema(
        {
            "id": text,
            "claim": text,
            "status": {"type": "string", "enum": ["observed", "inferred"]},
            "evidence": {"type": "array", "minItems": 1, "items": evidence},
        }
    )
    uncertainty = _strict_schema(
        {
            "id": text,
            "question": text,
            "why_it_matters": text,
            "resolution": text,
        }
    )
    behavior = _strict_schema(
        {
            "id": text,
            "behavior": text,
            "derived_from": {"type": "array", "minItems": 1, "items": text},
            "rationale": text,
            "tradeoffs": {"type": "array", "items": text},
        }
    )
    schema = _strict_schema(
        {
            "environment_observations": {
                "type": "array",
                "minItems": 1,
                "items": observation,
            },
            "environment_uncertainties": {"type": "array", "items": uncertainty},
            "successful_policy_behaviors": {
                "type": "array",
                "minItems": 1,
                "items": behavior,
            },
        }
    )
    Draft202012Validator.check_schema(schema)
    return schema


def research_schema(
    manifest: EvaluatorManifest,
    *,
    behavior_ids: Sequence[str] | None = None,
    ledger_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    citation_ids = (
        {"type": "string", "enum": list(ledger_ids)}
        if ledger_ids
        else text
    )
    citations = {
        "type": "array",
        "items": _strict_schema(
            {
                "entry_id": citation_ids,
                "bearing": {
                    "type": "string",
                    "enum": ["supports", "contradicts", "unresolved"],
                },
                "finding": text,
            }
        ),
    }
    if ledger_ids is not None and not ledger_ids:
        citations["maxItems"] = 0
    lineage_options = [
        _strict_schema(
            {
                "kind": {"type": "string", "const": "new"},
                "prior_entry_id": {"type": "null"},
            }
        )
    ]
    if ledger_ids is None or ledger_ids:
        lineage_options.append(
            _strict_schema(
                {
                    "kind": {"type": "string", "const": "refinement"},
                    "prior_entry_id": (
                        {"type": "string", "enum": list(ledger_ids)}
                        if ledger_ids is not None
                        else text
                    ),
                }
            )
        )
    telemetry = {
        name: {"type": ["string", "null"], "minLength": 1}
        for name in manifest.public_telemetry
    }
    return _strict_schema(
        {
            "strategy_behavior_id": (
                {"type": "string", "enum": list(behavior_ids)}
                if behavior_ids is not None
                else text
            ),
            "claim": text,
            "mechanism": text,
            "viability": text,
            "evidence_review": _strict_schema(
                {
                    "summary": text,
                    "citations": citations,
                }
            ),
            "expected_effect": text,
            "expected_telemetry": _strict_schema(telemetry),
            "falsifiers": {"type": "array", "minItems": 1, "items": text},
            "lineage": {"anyOf": lineage_options},
        }
    )


def planning_schema(
    manifest: EvaluatorManifest,
    *,
    behavior_ids: Sequence[str] | None = None,
    ledger_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    direction = _strict_schema(
        {
            "strategy_behavior_id": (
                {"type": "string", "enum": list(behavior_ids)}
                if behavior_ids is not None
                else text
            ),
            "champion_assessment": text,
            "remaining_gap": text,
            "disposition": {
                "type": "string",
                "enum": ["candidate", "exhausted"],
            },
            "request": {
                "anyOf": [
                    research_schema(
                        manifest,
                        behavior_ids=behavior_ids,
                        ledger_ids=ledger_ids,
                    ),
                    {"type": "null"},
                ]
            },
            "evidence": {"type": "array", "minItems": 1, "items": text},
            "feasibility": text,
            "expected_value": text,
        }
    )
    return _strict_schema(
        {
            "directions": {"type": "array", "minItems": 1, "items": direction},
            "selection_rationale": text,
            "selection": (
                {
                    "anyOf": [
                        {"type": "string", "enum": list(behavior_ids)},
                        {"type": "null"},
                    ]
                }
                if behavior_ids is not None
                else {"type": ["string", "null"], "minLength": 1}
            ),
        }
    )


def validate_planning(
    value: Mapping[str, Any],
    *,
    strategy: Mapping[str, Any],
    ledger: Sequence[Mapping[str, Any]],
    manifest: EvaluatorManifest,
) -> dict[str, Any] | None:
    expected = {
        item["id"] for item in strategy["successful_policy_behaviors"]
    }
    directions = value["directions"]
    actual = [item["strategy_behavior_id"] for item in directions]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValidationError("planner must assess every strategy behavior exactly once")
    requests: dict[str, Mapping[str, Any]] = {}
    for item in directions:
        request = item["request"]
        if (item["disposition"] == "candidate") != (
            request is not None
        ):
            raise ValidationError("planner direction disposition and request disagree")
        if request is None:
            continue
        normalized_request = dict(request)
        telemetry = normalized_request.get("expected_telemetry")
        if isinstance(telemetry, Mapping):
            normalized_request["expected_telemetry"] = {
                name: expectation
                for name, expectation in telemetry.items()
                if expectation is not None
            }
        ResearchRequest.from_mapping(
            normalized_request,
            allowed_telemetry=manifest.public_telemetry,
        )
        if (
            normalized_request["strategy_behavior_id"]
            != item["strategy_behavior_id"]
        ):
            raise ValidationError("planner request belongs to a different direction")
        validate_research_links(normalized_request, strategy=strategy, ledger=ledger)
        requests[item["strategy_behavior_id"]] = normalized_request
    selection = value["selection"]
    if selection is None:
        if any(item["disposition"] != "exhausted" for item in directions):
            raise ValidationError(
                "planner may omit selection only when all directions are exhausted"
            )
        return None
    if selection not in expected:
        raise ValidationError("planner selected an unknown direction")
    if selection not in requests:
        raise ValidationError("planner selected an exhausted direction")
    return dict(requests[selection])


def _entry_files(task_directory: Path) -> list[Path]:
    root = task_directory / "exploration" / "entries"
    return sorted(root.glob("*.public.json")) if root.is_dir() else []


def load_ledger(task_directory: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in _entry_files(task_directory):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"exploration entry is invalid: {path}") from error
        if not isinstance(value, dict):
            raise StateError(f"exploration entry is invalid: {path}")
        entries.append(value)
    return entries


def add_ledger_entry(task_directory: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    entries = load_ledger(task_directory)
    source = entry.get("source")
    for saved in entries:
        if saved.get("source") == source:
            expected = {**entry, "entry_id": saved["entry_id"]}
            if saved != expected:
                raise StateError(f"exploration source changed: {source}")
            return saved
    identifier = f"entry-{len(entries) + 1:06d}"
    value = {**entry, "entry_id": identifier}
    write_json_once(
        task_directory / "exploration" / "entries" / f"{identifier}.public.json",
        value,
    )
    rebuild_catalog(task_directory)
    return value


def _rebuild_ledger(task_directory: Path) -> None:
    lines = [
        json.dumps(
            _catalog_entry(task_directory, entry),
            sort_keys=True,
            separators=(",", ":"),
        )
        for entry in load_ledger(task_directory)
    ]
    atomic_write_text(
        task_directory / "exploration" / "ledger.public.jsonl",
        "\n".join(lines) + ("\n" if lines else ""),
    )


def rebuild_catalog(task_directory: Path) -> None:
    """Regenerate bounded public indexes from canonical exploration entries."""
    _rebuild_ledger(task_directory)
    exhaustion = [
        {
            "entry_id": entry["entry_id"],
            "source": entry["source"],
            "summary": entry["message"],
            "directions": [
                {
                    "strategy_behavior_id": direction["strategy_behavior_id"],
                    "evidence": direction["evidence"],
                }
                for direction in entry.get("planning", {}).get("directions", [])
            ],
        }
        for entry in load_ledger(task_directory)
        if entry.get("rejection_code") == "directions_exhausted"
    ]
    atomic_write_text(
        task_directory / "exploration" / "direction-exhaustion.public.jsonl",
        "\n".join(
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in exhaustion
        )
        + ("\n" if exhaustion else ""),
    )


def _catalog_entry(task_directory: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    catalog = {
        key: entry[key]
        for key in (
            "entry_id",
            "source",
            "kind",
            "strategy_behavior_id",
            "decision",
            "operational_status",
            "scientific_status",
            "reason_code",
            "rejection_code",
            "claim",
            "mechanism",
            "message",
            "lineage",
            "changed_paths",
        )
        if entry.get(key) is not None
    }
    behaviors = entry.get("successful_policy_behaviors")
    if isinstance(behaviors, list):
        catalog["strategy_behavior_ids"] = [
            item.get("id") for item in behaviors if isinstance(item, Mapping)
        ]
    reflection = entry.get("reflection")
    if isinstance(reflection, Mapping):
        assessment = reflection.get("assessment")
        compact_reflection: dict[str, Any] = {
            "status": reflection.get("status"),
            "warning": reflection.get("warning"),
        }
        if isinstance(assessment, Mapping):
            compact = {
                    "summary": assessment.get("summary"),
                    "strategy_realization": (
                        assessment.get("strategy_behavior", {}).get("realization")
                        if isinstance(assessment.get("strategy_behavior"), Mapping)
                        else None
                    ),
                    "mechanism_status": (
                        assessment.get("mechanism", {}).get("status")
                        if isinstance(assessment.get("mechanism"), Mapping)
                        else None
                    ),
                    "implementation_status": (
                        assessment.get("implementation", {}).get("status")
                        if isinstance(assessment.get("implementation"), Mapping)
                        else None
                    ),
                    "next_action": (
                        assessment.get("next_action", {}).get("kind")
                        if isinstance(assessment.get("next_action"), Mapping)
                        else None
                    ),
                    "metric_findings": [
                        {
                            "metric": signal.get("metric"),
                            "finding": signal.get("finding"),
                        }
                        for signal in assessment.get("material_signals", [])
                        if isinstance(signal, Mapping)
                    ],
                }
            compact_reflection.update(compact)
        catalog["reflection"] = {
            key: value for key, value in compact_reflection.items() if value is not None
        }
    source = entry.get("source")
    if isinstance(source, str) and source.startswith("experiment:"):
        result_path = (
            task_directory
            / "experiments"
            / source.removeprefix("experiment:")
            / "result.public.json"
        )
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                result = canonical_result(result)
                comparisons = result["evaluation"]["comparisons"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
                raise StateError(f"published result is invalid: {result_path}") from error
            if comparisons:
                comparison = comparisons[-1]
                catalog["effect_estimate"] = comparison["effect_estimate"]
                catalog["one_sided_lower_bound"] = comparison["one_sided_lower_bound"]
            catalog.update(
                operational_status=result["operational_status"],
                scientific_status=result["scientific_status"],
                reason_code=result["reason_code"],
            )
    catalog["entry_path"] = str(
        Path("entries") / f"{entry['entry_id']}.public.json"
    )
    return catalog


def search_ledger(
    task_directory: Path,
    *,
    query: str | None = None,
    path: str | None = None,
    decision: str | None = None,
) -> list[dict[str, Any]]:
    words = tuple((query or "").casefold().split())
    matches = []
    for entry in load_ledger(task_directory):
        text = json.dumps(entry, sort_keys=True).casefold()
        changed_paths = entry.get("changed_paths", [])
        if words and not all(word in text for word in words):
            continue
        if path and not any(
            fnmatchcase(item, path) for item in changed_paths if isinstance(item, str)
        ):
            continue
        if decision and entry.get("decision") != decision:
            continue
        matches.append(entry)
    return matches


def _latest_strategy(task_directory: Path) -> tuple[int, dict[str, Any]] | None:
    revisions = sorted(
        (task_directory / "strategy").glob(
            "[0-9][0-9][0-9][0-9][0-9][0-9].public.json"
        )
    )
    if not revisions:
        return None
    path = revisions[-1]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"saved strategy is invalid: {path}") from error
    if not isinstance(value, dict):
        raise StateError(f"saved strategy is invalid: {path}")
    try:
        Draft202012Validator(strategy_schema()).validate(value)
    except JsonSchemaError as error:
        raise StateError(f"saved strategy is invalid: {path}") from error
    return int(path.stem.split(".")[0]), value


def _runtime_paths(task: LocatedTask) -> tuple[Path, ...]:
    paths: list[Path] = []
    for command in task.config.environment_probes:
        paths.extend(command_runtime_read_paths(command))
    return tuple(dict.fromkeys(paths))


def _environment_packet(task: LocatedTask) -> list[dict[str, Any]]:
    packet = []
    for source in task.config.environment_sources:
        item: dict[str, Any] = {
            "id": source.identifier,
            "kind": source.kind,
            "description": source.description,
        }
        if source.path is not None:
            runtime_path = (
                task.directory / "environment" / source.identifier
                if source.commit is not None
                else source.path
            )
            item["path"] = str(runtime_path)
            item["include"] = list(source.include)
            if source.commit is not None:
                item["commit"] = source.commit
        else:
            item["command"] = list(source.command)
            item["backed_by"] = list(source.backed_by)
        packet.append(item)
    return packet


def validate_strategy_links(value: Mapping[str, Any], *, source_ids: set[str]) -> None:
    observations = value["environment_observations"]
    observation_ids = [item["id"] for item in observations]
    if len(observation_ids) != len(set(observation_ids)):
        raise StateError("strategy environment observation ids must be unique")
    for observation in observations:
        for evidence in observation["evidence"]:
            if evidence["source_id"] not in source_ids:
                raise StateError("strategy cites an unknown environment source")
    uncertainty_ids = [item["id"] for item in value["environment_uncertainties"]]
    if len(uncertainty_ids) != len(set(uncertainty_ids)):
        raise StateError("strategy uncertainty ids must be unique")
    grounding_ids = {*observation_ids, *uncertainty_ids}
    if len(grounding_ids) != len(observation_ids) + len(uncertainty_ids):
        raise StateError("strategy observation and uncertainty ids must be distinct")
    behavior_ids = [item["id"] for item in value["successful_policy_behaviors"]]
    if len(behavior_ids) != len(set(behavior_ids)):
        raise StateError("strategy behavior ids must be unique")
    for behavior in value["successful_policy_behaviors"]:
        if not set(behavior["derived_from"]) <= grounding_ids:
            raise StateError("strategy behavior cites unknown environment grounding")


def validate_research_links(
    request: Mapping[str, Any],
    *,
    strategy: Mapping[str, Any],
    ledger: Sequence[Mapping[str, Any]],
) -> None:
    behaviors = {item["id"] for item in strategy["successful_policy_behaviors"]}
    if request["strategy_behavior_id"] not in behaviors:
        raise ValidationError("research request names an unknown strategy behavior")
    identifiers = {entry["entry_id"] for entry in ledger}
    lineage = request["lineage"]
    if lineage["kind"] == "new" and lineage["prior_entry_id"] is not None:
        raise ValidationError("new research request must not name a prior ledger entry")
    if (
        lineage["kind"] == "refinement"
        and lineage["prior_entry_id"] not in identifiers
    ):
        raise ValidationError("research refinement names an unknown ledger entry")
    if any(
        citation["entry_id"] not in identifiers
        for citation in request["evidence_review"]["citations"]
    ):
        raise ValidationError("research evidence review names an unknown ledger entry")
    cited = {
        citation["entry_id"] for citation in request["evidence_review"]["citations"]
    }
    if lineage["kind"] == "refinement" and lineage["prior_entry_id"] not in cited:
        raise ValidationError("research refinement must cite its prior ledger entry")
    by_id = {entry["entry_id"]: entry for entry in ledger}
    for citation in request["evidence_review"]["citations"]:
        prior = by_id[citation["entry_id"]]
        if (
            prior.get("scientific_status") == "untested"
            and citation["bearing"] != "unresolved"
        ):
            raise ValidationError(
                "untested experiments cannot support or contradict performance"
            )
        if (
            prior.get("scientific_status") == "inconclusive"
            and citation["bearing"] != "unresolved"
        ):
            raise ValidationError(
                "inconclusive experiments cannot support or contradict performance"
            )


def ensure_strategy(
    task: LocatedTask,
    worktree: Path,
    manifest: EvaluatorManifest,
    *,
    refresh: bool,
    command_builder: AgentCommandBuilder | None,
    stop_path: Path,
) -> tuple[int, dict[str, Any]]:
    assert task.config.method is not None
    task.config.method.require_component("strategize", "strategize.environment")
    latest = _latest_strategy(task.directory)
    if latest is not None and not refresh:
        return latest
    strategy_root = task.directory / "strategy"
    pending = sorted(
        path
        for path in strategy_root.glob("[0-9][0-9][0-9][0-9][0-9][0-9]")
        if (path / "agent-selection.public.json").is_file()
        and not (strategy_root / f"{path.name}.public.json").is_file()
    )
    revision = (
        int(pending[-1].name)
        if pending
        else max(
            [latest[0] if latest else 0]
            + [
                int(path.name)
                for path in strategy_root.glob("[0-9][0-9][0-9][0-9][0-9][0-9]")
            ]
        )
        + 1
    )
    root = task.directory / "strategy" / f"{revision:06d}"
    prior_attempts = sorted((root / "attempts").glob("[0-9][0-9][0-9][0-9]"))
    attempt = root / "attempts" / f"{len(prior_attempts) + 1:04d}"
    scratch = attempt / "output"
    scratch.mkdir(parents=True, exist_ok=True)
    schema = scratch / "strategy.schema.json"
    source_ids = tuple(source.identifier for source in task.config.environment_sources)
    write_json_once(schema, strategy_schema(source_ids=source_ids))
    prompt = (
        "Build an environment-grounded strategy before any candidate is selected. "
        "Analyze only the declared environment implementation, interface, documentation, "
        "and probes. Treat the objective as a goal boundary, not as evidence about how "
        "the environment works. Separate observed facts from inference, cite every "
        "environment observation to a declared source and location, and derive high-level "
        "behaviors a successful policy should exhibit from those observations. Assign "
        "unique IDs across observations and uncertainties, assign unique behavior IDs, "
        "and use only declared observation or uncertainty IDs in derived_from. Do not "
        "inspect or diagnose the current policy, propose algorithms or code changes, name "
        "weights or telemetry targets, restate evaluator criteria as behaviors, implement "
        "anything, or commit. On refresh, use the public exhaustion summaries to avoid "
        "merely republishing directions already exhausted against the current champion. "
        "Return only the required strategy JSON.\n\n"
        + json.dumps(
            {
                "evaluation_boundary": {"objective": task.config.objective},
                "environment_sources": _environment_packet(task),
                "prior_strategy": latest[1] if latest else None,
                "refresh_reason": "candidate search misses" if refresh else "initial analysis",
                "direction_exhaustion": (
                    str(
                        task.directory
                        / "exploration"
                        / "direction-exhaustion.public.jsonl"
                    )
                    if refresh
                    else None
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if command_builder is None:
        exhaustion_path = (
            task.directory / "exploration" / "direction-exhaustion.public.jsonl"
        )
        assert task.config.method is not None
        agent = select_agent(
            task.config.method,
            component="strategize",
            lifecycle=f"strategy:{revision:06d}",
            root=root,
        )
        read_paths = (
                *_runtime_paths(task),
                *(
                    (
                        task.directory / "environment" / source.identifier
                        if source.commit is not None
                        else source.path
                    )
                    for source in task.config.environment_sources
                    if source.path is not None
                ),
                *((exhaustion_path,) if refresh and exhaustion_path.is_file() else ()),
            )
        try:
            command = agent_command(
                agent,
                AgentSessionRequest(
                    worktree=worktree,
                    scratch=scratch,
                    output_schema=schema,
                    prompt=prompt,
                    output_name="strategy.public.json",
                    read_paths=read_paths,
                    writable_worktree=False,
                    read_worktree=False,
                ),
            )
        except (OSError, StateError) as error:
            write_json_once(
                root / "strategy.failure.json",
                {"message": str(error)},
            )
            raise
        try:
            environment = agent_environment(
                agent,
                credential_home=Path(
                    os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
                ),
                writable_home=scratch,
            )
        except (OSError, StateError) as error:
            if not (root / "strategy.failure.json").exists():
                write_json_once(
                    root / "strategy.failure.json",
                    {"message": str(error)},
                )
            raise
        write_json_once(
            attempt / "agent.public.json",
            agent_provenance(agent, lifecycle=f"strategy:{revision:06d}"),
        )
    else:
        try:
            command = command_builder(worktree, scratch, schema, prompt)
        except (OSError, StateError) as error:
            write_json_once(
                root / "strategy.failure.json",
                {"message": str(error)},
            )
            raise
        environment = None
    process_directory = attempt / "process"
    try:
        result = run_or_load_once(
            process_directory,
            command,
            timeout_seconds=3600,
            max_output_bytes=2_000_000,
            cwd=worktree,
            env=environment,
            stop_path=stop_path,
            stdin_path=(
                agent_prompt_path(scratch) if command_builder is None else None
            ),
        )
        if result["return_code"] != 0:
            transient = transient_process_error(
                process_directory,
                stage="strategy",
                codex=command_builder is None,
            )
            if transient is not None:
                raise transient
            raise StateError("fresh strategy session exited unsuccessfully")
    except StoppedError:
        raise
    except ProcessError as error:
        if isinstance(error, TransientDownstreamError):
            failure = error
        else:
            failure = transient_process_error(
                process_directory,
                stage="strategy",
                codex=command_builder is None,
                fallback=str(error),
            )
        if failure is not None:
            if not (root / "strategy.failure.json").exists():
                write_json_once(
                    root / "strategy.failure.json",
                    {"message": str(failure)},
                )
            raise failure
        if not (root / "strategy.failure.json").exists():
            write_json_once(
                root / "strategy.failure.json",
                {"message": str(error)},
            )
        raise
    except StateError as error:
        if not (root / "strategy.failure.json").exists():
            write_json_once(
                root / "strategy.failure.json",
                {"message": str(error)},
            )
        raise
    output = scratch / "strategy.public.json"
    try:
        value = json.loads(output.read_text(encoding="utf-8"))
        Draft202012Validator(strategy_schema(source_ids=source_ids)).validate(value)
        validate_strategy_links(
            value,
            source_ids={source.identifier for source in task.config.environment_sources},
        )
    except (OSError, json.JSONDecodeError, JsonSchemaError, StateError) as error:
        detail = error.message if isinstance(error, JsonSchemaError) else str(error)
        failure = StateError(
            f"fresh strategy session did not write valid strategy JSON: {detail}"
        )
        if not (root / "strategy.failure.json").exists():
            write_json_once(
                root / "strategy.failure.json",
                {"message": str(failure)},
            )
        raise failure from error
    published = task.directory / "strategy" / f"{revision:06d}.public.json"
    write_json_once(published, value)
    add_ledger_entry(
        task.directory,
        {
            "source": f"strategy:{revision:06d}",
            "kind": "strategy",
            "strategy_revision": revision,
            "agent": agent.name if command_builder is None else None,
            "model": agent.model if command_builder is None else task.config.strategy_model,
            "reasoning_effort": (
                agent.reasoning_effort
                if command_builder is None
                else task.config.strategy_reasoning_effort
            ),
            "successful_policy_behaviors": value["successful_policy_behaviors"],
        },
    )
    return revision, value


def next_search_id(task_directory: Path) -> int:
    paths = sorted(
        (task_directory / "searches").glob("[0-9][0-9][0-9][0-9][0-9][0-9]")
    )
    return int(paths[-1].name) + 1 if paths else 1


def record_miss(
    task: LocatedTask,
    *,
    search_id: int,
    attempt: int,
    champion: str,
    request: Mapping[str, Any] | None,
    miss: ResearchMiss,
    changed_paths: Sequence[str] = (),
    planning: Mapping[str, Any] | None = None,
) -> None:
    add_ledger_entry(
        task.directory,
        {
            "source": f"search:{search_id:06d}:attempt:{attempt:02d}",
            "kind": "research_miss",
            "champion": champion,
            "claim": request.get("claim") if request else None,
            "mechanism": request.get("mechanism") if request else None,
            "strategy_behavior_id": (
                request.get("strategy_behavior_id") if request else None
            ),
            "evidence_review": request.get("evidence_review") if request else None,
            "lineage": request.get("lineage") if request else None,
            "changed_paths": list(changed_paths),
            "rejection_code": miss.code,
            "message": str(miss),
            **miss.details,
            **({"planning": dict(planning)} if planning is not None else {}),
        },
    )
