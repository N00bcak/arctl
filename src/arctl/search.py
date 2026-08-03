"""Public strategy, exploration history, and bounded candidate discovery records."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

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
from .process import run_or_load_once
from .registry import LocatedTask
from .sandbox import command_runtime_read_paths, research_command, sanitized_environment
from .storage import atomic_write_text, write_json_once

AgentCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]


def _strict_schema(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": dict(properties),
    }


def strategy_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    evidence = _strict_schema(
        {
            "source_id": text,
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
            "schema_version": {"type": "integer", "const": 2},
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


def research_schema(manifest: EvaluatorManifest) -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    telemetry = {
        name: {"type": ["string", "null"], "minLength": 1}
        for name in manifest.public_telemetry
    }
    return _strict_schema(
        {
            "schema_version": {"type": "integer", "const": 2},
            "strategy_behavior_id": text,
            "claim": text,
            "mechanism": text,
            "viability": text,
            "evidence_review": _strict_schema(
                {
                    "summary": text,
                    "citations": {
                        "type": "array",
                        "items": _strict_schema(
                            {
                                "entry_id": text,
                                "bearing": {
                                    "type": "string",
                                    "enum": ["supports", "contradicts", "unresolved"],
                                },
                                "finding": text,
                            }
                        ),
                    },
                }
            ),
            "expected_effect": text,
            "expected_telemetry": _strict_schema(telemetry),
            "falsifiers": {"type": "array", "minItems": 1, "items": text},
            "lineage": _strict_schema(
                {
                    "kind": {"type": "string", "enum": ["new", "refinement"]},
                    "prior_entry_id": {"type": ["string", "null"]},
                }
            ),
        }
    )


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
    _rebuild_ledger(task_directory)
    return value


def _rebuild_ledger(task_directory: Path) -> None:
    lines = [
        json.dumps(entry, sort_keys=True, separators=(",", ":"))
        for entry in load_ledger(task_directory)
    ]
    atomic_write_text(
        task_directory / "exploration" / "ledger.public.jsonl",
        "\n".join(lines) + ("\n" if lines else ""),
    )


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
            item["path"] = str(source.path)
            item["include"] = list(source.include)
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
    behavior_ids = [item["id"] for item in value["successful_policy_behaviors"]]
    if len(behavior_ids) != len(set(behavior_ids)):
        raise StateError("strategy behavior ids must be unique")
    known_observations = set(observation_ids)
    for behavior in value["successful_policy_behaviors"]:
        if not set(behavior["derived_from"]) <= known_observations:
            raise StateError("strategy behavior cites an unknown observation")
    uncertainty_ids = [item["id"] for item in value["environment_uncertainties"]]
    if len(uncertainty_ids) != len(set(uncertainty_ids)):
        raise StateError("strategy uncertainty ids must be unique")


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


def ensure_strategy(
    task: LocatedTask,
    worktree: Path,
    manifest: EvaluatorManifest,
    *,
    refresh: bool,
    command_builder: AgentCommandBuilder | None,
    stop_path: Path,
) -> tuple[int, dict[str, Any]]:
    latest = _latest_strategy(task.directory)
    if latest is not None and not refresh:
        return latest
    failed = [
        int(path.parent.name)
        for path in (task.directory / "strategy").glob(
            "[0-9][0-9][0-9][0-9][0-9][0-9]/strategy.failure.json"
        )
    ]
    revision = max([latest[0] if latest else 0, *failed]) + 1
    root = task.directory / "strategy" / f"{revision:06d}"
    scratch = root / "output"
    scratch.mkdir(parents=True, exist_ok=True)
    schema = scratch / "strategy.schema.json"
    write_json_once(schema, strategy_schema())
    prompt = (
        "Build an environment-grounded strategy before any candidate is selected. "
        "Analyze only the declared environment implementation, interface, documentation, "
        "and probes. Treat the objective as a goal boundary, not as evidence about how "
        "the environment works. Separate observed facts from inference, cite every "
        "environment observation to a declared source and location, and derive high-level "
        "behaviors a successful policy should exhibit from those observations. Do not "
        "inspect or diagnose the current policy, propose algorithms or code changes, name "
        "weights or telemetry targets, restate evaluator criteria as behaviors, implement "
        "anything, or commit. Return only the required strategy JSON.\n\n"
        + json.dumps(
            {
                "evaluation_boundary": {"objective": task.config.objective},
                "environment_sources": _environment_packet(task),
                "prior_strategy": latest[1] if latest else None,
                "refresh_reason": "candidate search misses" if refresh else "initial analysis",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if command_builder is None:
        command = research_command(
            worktree=worktree,
            scratch=scratch,
            output_schema=schema,
            prompt=prompt,
            output_name="strategy.public.json",
            read_paths=(
                *_runtime_paths(task),
                *(
                    source.path
                    for source in task.config.environment_sources
                    if source.path is not None
                ),
            ),
            model=task.config.strategy_model,
            reasoning_effort=task.config.strategy_reasoning_effort,
            writable_worktree=False,
            read_worktree=False,
        )
        environment = sanitized_environment(
            codex_home=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
            writable_home=scratch,
        )
    else:
        command = command_builder(worktree, scratch, schema, prompt)
        environment = None
    process_directory = root / "process"
    try:
        result = run_or_load_once(
            process_directory,
            command,
            timeout_seconds=3600,
            max_output_bytes=2_000_000,
            cwd=worktree,
            env=environment,
            stop_path=stop_path,
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
            write_json_once(
                root / "strategy.failure.json",
                {"schema_version": 1, "message": str(failure)},
            )
            raise failure
        write_json_once(
            root / "strategy.failure.json",
            {"schema_version": 1, "message": str(error)},
        )
        raise
    except StateError as error:
        write_json_once(
            root / "strategy.failure.json",
            {"schema_version": 1, "message": str(error)},
        )
        raise
    output = scratch / "strategy.public.json"
    try:
        value = json.loads(output.read_text(encoding="utf-8"))
        Draft202012Validator(strategy_schema()).validate(value)
        validate_strategy_links(
            value,
            source_ids={source.identifier for source in task.config.environment_sources},
        )
    except (OSError, json.JSONDecodeError, JsonSchemaError, StateError) as error:
        failure = StateError("fresh strategy session did not write valid strategy JSON")
        write_json_once(
            root / "strategy.failure.json",
            {"schema_version": 1, "message": str(failure)},
        )
        raise failure from error
    published = task.directory / "strategy" / f"{revision:06d}.public.json"
    write_json_once(published, value)
    add_ledger_entry(
        task.directory,
        {
            "schema_version": 1,
            "source": f"strategy:{revision:06d}",
            "kind": "strategy",
            "strategy_revision": revision,
            "model": task.config.strategy_model,
            "reasoning_effort": task.config.strategy_reasoning_effort,
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
) -> None:
    add_ledger_entry(
        task.directory,
        {
            "schema_version": 1,
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
        },
    )
