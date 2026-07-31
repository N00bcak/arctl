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

from .errors import ProcessError, ResearchMiss, StateError, StoppedError
from .manifest import EvaluatorManifest
from .models import ResearchRequest
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
    direction = _strict_schema(
        {
            "id": text,
            "title": text,
            "rationale": text,
            "mechanism": text,
            "observable_signal": text,
            "risks": {"type": "array", "items": text},
        }
    )
    schema = _strict_schema(
        {
            "schema_version": {"type": "integer", "const": 1},
            "environment_signals": {"type": "array", "minItems": 1, "items": text},
            "success_profile": {"type": "array", "minItems": 1, "items": text},
            "uncertainties": {"type": "array", "items": text},
            "directions": {"type": "array", "minItems": 1, "items": direction},
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
            "schema_version": {"type": "integer", "const": 1},
            "claim": text,
            "mechanism": text,
            "expected_effect": text,
            "expected_telemetry": _strict_schema(telemetry),
            "falsifiers": {"type": "array", "minItems": 1, "items": text},
            "direction": _strict_schema(
                {
                    "kind": {"type": "string", "enum": ["new", "refinement"]},
                    "prior_entry_id": {"type": ["string", "null"]},
                    "strategy_direction_id": text,
                    "rationale": text,
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
    for command in (*task.config.public_checks, task.config.public_probe):
        paths.extend(command_runtime_read_paths(command))
    return tuple(dict.fromkeys(paths))


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
    ledger = task.directory / "exploration" / "ledger.public.jsonl"
    prompt = (
        "Analyze this public research environment before any candidate is selected. "
        "Do not implement or commit a candidate. Use public checks or the public probe "
        "only when useful. Identify the qualities of a successful policy, the signals "
        "the environment exposes, uncertainties, and several distinct strategic "
        "directions. Search the exploration ledger before recommending repeated work. "
        "Return only the required strategy JSON.\n\n"
        + json.dumps(
            {
                "objective": task.config.objective,
                "public_checks": [list(command) for command in task.config.public_checks],
                "public_probe": list(task.config.public_probe),
                "statistic": manifest.public_statistic,
                "subject_interface": manifest.subject_interface,
                "prior_strategy": latest[1] if latest else None,
                "ledger_path": str(ledger) if ledger.is_file() else None,
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
            read_paths=(*_runtime_paths(task), *((ledger,) if ledger.is_file() else ())),
            model=task.config.strategy_model,
            reasoning_effort=task.config.strategy_reasoning_effort,
        )
        environment = sanitized_environment(
            codex_home=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
            writable_home=scratch,
        )
    else:
        command = command_builder(worktree, scratch, schema, prompt)
        environment = None
    try:
        result = run_or_load_once(
            root / "process",
            command,
            timeout_seconds=3600,
            max_output_bytes=2_000_000,
            cwd=worktree,
            env=environment,
            stop_path=stop_path,
        )
        if result["return_code"] != 0:
            raise StateError("fresh strategy session exited unsuccessfully")
    except StoppedError:
        raise
    except (ProcessError, StateError) as error:
        write_json_once(
            root / "strategy.failure.json",
            {"schema_version": 1, "message": str(error)},
        )
        raise
    output = scratch / "strategy.public.json"
    try:
        value = json.loads(output.read_text(encoding="utf-8"))
        Draft202012Validator(strategy_schema()).validate(value)
    except (OSError, json.JSONDecodeError, JsonSchemaError) as error:
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
            "directions": value["directions"],
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
            "direction": request.get("direction") if request else None,
            "changed_paths": list(changed_paths),
            "rejection_code": miss.code,
            "message": str(miss),
        },
    )
