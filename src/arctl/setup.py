"""Resumable, pre-approval Python research-workspace setup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

from .agent_backend import AgentSessionRequest, agent_command, agent_environment
from .errors import StateError, ValidationError
from .manifest import EvaluatorManifest
from .methods import AgentDefinition
from .models import TaskConfig, validate_task_id
from .process import run_or_load_once
from .storage import atomic_write_json, atomic_write_text
from .taskio import load_task

SetupCommandBuilder = Callable[[Path, Path, Path, str], Sequence[str]]
SetupProgress = Callable[[Mapping[str, Any]], None]

QUESTION_IDS = (
    "objective",
    "policy_boundary",
    "environment_boundary",
    "independent_trial",
    "outcome",
    "hidden_data",
    "hard_rules",
    "randomness",
    "telemetry",
    "runtime_budget",
    "evaluator_pattern",
)
QUESTION_GROUPS = {
    "target": ("objective", "outcome"),
    "trial_protocol": ("independent_trial", "hidden_data", "randomness"),
    "constraints": ("hard_rules", "runtime_budget"),
    "evaluator": ("telemetry", "evaluator_pattern"),
}
SETUP_CONTROLLER_CONTRACT = {
    "comparison": (
        "For each experiment, freeze the current accepted champion as comparator "
        "before reserving one ordered paired batch; run each arm once on that batch."
    ),
    "seeds": (
        "Controller-reserved calibration, primary, and suspect seeds never overlap. "
        "The evaluator materializes identical paired cases for both arms without "
        "exposing illegitimate hidden seed information to the policy."
    ),
    "calibration": (
        "When trials are automatic, evaluate the initial champion once at the ladder "
        "ceiling and choose the smallest rung whose approved diagnostic and every "
        "larger rung satisfy the approved threshold; otherwise use the ceiling."
    ),
    "decision": (
        "Positive lower bound accepts; positive effect with non-positive lower bound "
        "archives; non-positive effect rejects. Only ACCEPT promotes."
    ),
    "failure": (
        "Illegal output, exceptions, timeout, resource exhaustion, malformed evidence, "
        "or evaluator failure are operational failures with no statistical score; "
        "they are never imputed as bad trial outcomes."
    ),
}
SETUP_BUILD_CONTRACT = {
    "task_required": [
        "objective",
        "editable_paths",
        "denied_paths",
        "public_checks",
        "public_probe",
        "environment",
        "trials",
        "max_experiments",
    ],
    "task_controller_owned": [
        "schema_version",
        "task_id",
        "repo",
        "evaluator",
        "method",
    ],
    "public_probe": ["command", "trial_equivalents"],
    "environment": {
        "codebases": ["id", "description", "repo", "commit", "include"],
        "probes": ["id", "description", "command", "backed_by"],
    },
    "manifest": {
        "root": [
            "schema_version",
            "subject_command",
            "prepare_command",
            "calibrate_command",
            "score_command",
            "limits",
            "schemas",
            "public",
            "trial",
            "statistics",
            "variation",
            "suspect_test",
            "calibration",
        ],
        "limits": ["timeout_seconds", "max_output_bytes"],
        "schemas": ["public_case", "subject_result"],
        "public": ["statistic", "subject_interface", "telemetry"],
        "telemetry_metric": [
            "description",
            "unit",
            "scope",
            "role",
            "value_type",
            "direction",
        ],
        "trial": ["meaning", "dependence", "seed_to_case", "subject_visible_seed"],
        "statistics": ["score", "uncertainty", "positive_effect"],
        "variation": ["known", "mitigations"],
        "suspect_test": ["trigger", "reason_codes"],
        "calibration": ["supported", "policy", "ladder", "diagnostic"],
        "calibration_diagnostic": ["name", "units", "maximum"],
    },
}
_SETUP_TEMPLATE = """# ARCTL setup

<!-- Fill what you know. arctl will inspect the repository and ask only about
material gaps or conflicts. Delete no headings; empty sections are allowed. -->

## Goal and primary outcome

## Policy boundary

## Environment boundary

## Trial protocol

<!-- State the independent unit, fixed trial count or autocalibration ladder and
exact diagnostic/threshold, episode horizon, and any uncontrolled dependence.
arctl already owns pairing, comparator freezing, and seed non-reuse. -->

## Hidden information

## Hard constraints

## Telemetry

## Runtime budget

<!-- Give the timeout scope (per episode, arm batch, or whole comparison) and
only resource limits the intended runtime can actually enforce. -->

## Evaluator and success criterion

<!-- State the aggregate statistic and uncertainty method. arctl already owns
ACCEPT/ARCHIVE/REJECT mapping and treats operational failures as unscored. -->
"""
_PLACEHOLDERS = {
    "SETUP_SUBJECT_COMMIT",
    "SETUP_ENVIRONMENT_COMMIT",
    "SETUP_EVALUATOR_COMMIT",
}


def _git(repo: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise StateError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def _clean(repo: Path) -> bool:
    return not _git(repo, "status", "--porcelain", "--untracked-files=all")


def _clean_except_brief(repo: Path) -> bool:
    lines = _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    return all(line[3:] == "ARCTL_SETUP.md" for line in lines)


def _commit(repo: Path, message: str) -> str:
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        _git(repo, "add", "--all")
        _git(
            repo,
            "-c",
            "user.name=arctl setup",
            "-c",
            "user.email=setup@arctl.invalid",
            "commit",
            "-qm",
            message,
        )
    elif not _git(repo, "rev-parse", "--verify", "HEAD", check=False):
        _git(
            repo,
            "-c",
            "user.name=arctl setup",
            "-c",
            "user.email=setup@arctl.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            message,
        )
    return _git(repo, "rev-parse", "HEAD")


def _schema(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": dict(properties),
    }


def discovery_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    citation = _schema({"path": text, "location": text, "finding": text})
    question = _schema(
        {
            "id": {"type": "string", "enum": list(QUESTION_IDS)},
            "proposed_answer": text,
            "citations": {"type": "array", "items": citation},
            "source": {
                "type": "string",
                "enum": ["setup brief", "repository", "proposal"],
            },
        }
    )
    clarification = _schema(
        {
            "id": {"type": "string", "enum": list(QUESTION_GROUPS)},
            "prompt": text,
            "why": text,
            "proposed_answer": {"type": ["string", "null"]},
            "affected_fields": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": list(QUESTION_IDS)},
            },
        }
    )
    return _schema(
        {
            "schema_version": {"type": "integer", "const": 2},
            "brief_sha256": text,
            "summary": text,
            "fields": {
                "type": "array",
                "minItems": len(QUESTION_IDS),
                "maxItems": len(QUESTION_IDS),
                "items": question,
            },
            "open_questions": {
                "type": "array",
                "maxItems": len(QUESTION_GROUPS),
                "items": clarification,
            },
        }
    )


def _brief(setup: Mapping[str, Any]) -> tuple[Path, str, str]:
    subject = Path(setup["subject"])
    workspace = Path(setup["workspace"])
    subject_path = subject / "ARCTL_SETUP.md"
    path = subject_path if subject_path.is_file() else workspace / "ARCTL_SETUP.md"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return path, text, hashlib.sha256(text.encode()).hexdigest()


def setup_presentation(discovery: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable human/JSON view, including discovery-v1 consolidation."""
    if discovery.get("schema_version") == 2:
        return {
            "proposal": list(discovery["fields"]),
            "open_questions": list(discovery["open_questions"]),
        }
    fields = []
    unresolved: set[str] = set()
    markers = ("must confirm", "humans must", "should confirm", "need confirmation")
    for item in discovery.get("questions", []):
        fields.append({**item, "source": "repository"})
        if any(marker in item["proposed_answer"].lower() for marker in markers):
            unresolved.add(item["id"])
    questions = []
    by_id = {item["id"]: item for item in fields}
    for group, members in QUESTION_GROUPS.items():
        affected = [name for name in members if name in unresolved]
        if not affected:
            continue
        first = by_id[affected[0]]
        questions.append(
            {
                "id": group,
                "prompt": f"Confirm the {group.replace('_', ' ')}.",
                "why": "The earlier discovery marked these related details unresolved.",
                "proposed_answer": first["proposed_answer"],
                "affected_fields": affected,
            }
        )
    return {"proposal": fields, "open_questions": questions}


def brief_changed(setup: Mapping[str, Any], discovery: Mapping[str, Any]) -> bool:
    return discovery.get("schema_version") == 2 and discovery.get(
        "brief_sha256"
    ) != _brief(setup)[2]


def build_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    file_record = _schema({"path": text, "content": {"type": "string"}})
    files = {"type": "array", "items": file_record}
    return _schema(
        {
            "schema_version": {"type": "integer", "const": 2},
            "summary": text,
            "dependencies": {"type": "array", "items": text},
            "subject_files": files,
            "environment_files": files,
            "evaluator_files": files,
            "task": {"type": "object"},
            "evaluator_manifest": {"type": "object"},
        }
    )


def review_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    return _schema(
        {
            "schema_version": {"type": "integer", "const": 1},
            "summary": text,
            "findings": {
                "type": "array",
                "items": _schema(
                    {
                        "code": text,
                        "location": text,
                        "message": text,
                    }
                ),
            },
        }
    )


def _agent_run(
    *,
    root: Path,
    worktree: Path,
    schema_value: Mapping[str, Any],
    output_name: str,
    prompt: str,
    writable_worktree: bool,
    read_paths: tuple[Path, ...] = (),
    command_builder: SetupCommandBuilder | None = None,
    offline: bool = False,
    validate_output: bool = True,
) -> dict[str, Any]:
    scratch = root / "output"
    scratch.mkdir(parents=True, exist_ok=True)
    schema = root / "output.schema.json"
    atomic_write_json(schema, schema_value)
    if command_builder is None:
        agent = AgentDefinition(
            "setup-default", "codex-cli-v1", "gpt-5.6-sol", "medium"
        )
        command = agent_command(
            agent,
            AgentSessionRequest(
                worktree=worktree,
                scratch=scratch,
                output_schema=schema,
                prompt=prompt,
                output_name=output_name,
                writable_worktree=writable_worktree,
                read_paths=read_paths,
                network_enabled=not offline,
            ),
        )
        environment = agent_environment(
            agent,
            credential_home=Path(
                os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
            ),
            writable_home=scratch,
        )
    else:
        command = command_builder(worktree, scratch, schema, prompt)
        environment = None
    result = run_or_load_once(
        root / "process",
        command,
        timeout_seconds=3600,
        max_output_bytes=4_000_000,
        cwd=worktree,
        env=environment,
        stdin_path=root / "prompt.public.txt" if command_builder is None else None,
    )
    if result["return_code"] != 0:
        stderr_path = root / "process" / "stderr.bin"
        detail = (
            stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            if stderr_path.is_file()
            else ""
        )
        suffix = f": {detail}" if detail else ""
        raise StateError(f"setup agent failed{suffix}; inspect {root / 'process'}")
    try:
        value = json.loads((scratch / output_name).read_text(encoding="utf-8"))
        if validate_output:
            Draft202012Validator(schema_value).validate(value)
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"setup agent wrote invalid {output_name}: {error}") from error
    except JsonSchemaError as error:
        raise StateError(
            f"setup agent wrote invalid {output_name}: {error.message}"
        ) from error
    if not isinstance(value, dict):
        raise StateError(f"setup agent wrote invalid {output_name}")
    return value


def initialize_setup(
    *,
    data_root: Path,
    workspace: Path,
    repo: Path | None,
    new_repo: bool,
    task_id: str,
) -> dict[str, Any]:
    validate_task_id(task_id)
    workspace = workspace.resolve()
    task_directory = data_root.resolve() / "tasks" / task_id
    if task_directory.exists():
        raise StateError(f"task already exists: {task_id}")
    workspace.mkdir(parents=True, exist_ok=True)
    subject = (workspace / "subject") if new_repo else repo
    if subject is None:
        raise StateError("setup requires an existing or new subject repository")
    subject = subject.resolve()
    if new_repo:
        subject.mkdir(parents=True, exist_ok=False)
        _git(subject, "init", "-q")
        atomic_write_text(subject / "README.md", f"# {task_id}\n")
        _commit(subject, "Initialize research subject")
    elif not (subject / ".git").exists():
        raise StateError(f"target is not a Git worktree: {subject}")
    if not _clean_except_brief(subject):
        raise StateError("setup requires a clean subject Git worktree except ARCTL_SETUP.md")
    evaluator = workspace / "evaluator"
    environment = workspace / "environment"
    evaluator.mkdir()
    environment.mkdir()
    if not (subject / "ARCTL_SETUP.md").is_file() and not (
        workspace / "ARCTL_SETUP.md"
    ).exists():
        atomic_write_text(workspace / "ARCTL_SETUP.md", _SETUP_TEMPLATE)
    _git(evaluator, "init", "-q")
    _git(environment, "init", "-q")
    task_directory.mkdir(parents=True)
    record = {
        "schema_version": 1,
        "task_id": task_id,
        "workspace": str(workspace),
        "data_root": str(data_root.resolve()),
        "subject": str(subject),
        "subject_base": _git(subject, "rev-parse", "HEAD"),
        "environment": str(environment.resolve()),
        "evaluator": str(evaluator.resolve()),
        "new_repo": new_repo,
        "setup_brief": str(
            subject / "ARCTL_SETUP.md"
            if (subject / "ARCTL_SETUP.md").is_file()
            else workspace / "ARCTL_SETUP.md"
        ),
        "state": "DISCOVERY_REQUIRED",
    }
    atomic_write_json(task_directory / "setup.json", record)
    atomic_write_text(
        workspace / "arctl.workspace.yaml",
        "schema_version: 1\n"
        f"task_id: {json.dumps(task_id)}\n"
        f"data_root: {json.dumps(str(data_root.resolve()))}\n"
        f"subject: {json.dumps(str(subject))}\n"
        f"environment: {json.dumps(str(environment.resolve()))}\n"
        f"evaluator: {json.dumps(str(evaluator.resolve()))}\n",
    )
    _git(subject, "config", "--local", "arctl.dataRoot", str(data_root.resolve()))
    _git(subject, "config", "--local", "arctl.task", task_id)
    return record


def load_setup(data_root: Path, task_id: str | None) -> tuple[Path, dict[str, Any]]:
    tasks = data_root / "tasks"
    if task_id is None:
        matches = sorted(tasks.glob("*/setup.json")) if tasks.is_dir() else []
        if len(matches) != 1:
            raise StateError("setup task is missing or ambiguous; specify TASK")
        path = matches[0]
    else:
        validate_task_id(task_id)
        path = tasks / task_id / "setup.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("setup state is missing or invalid") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise StateError("setup state is missing or invalid")
    return path.parent, value


def _save_setup(directory: Path, value: dict[str, Any]) -> None:
    atomic_write_json(directory / "setup.json", value)


def discover_setup(
    directory: Path,
    setup: dict[str, Any],
    *,
    command_builder: SetupCommandBuilder | None = None,
    offline: bool = False,
    progress: SetupProgress | None = None,
) -> dict[str, Any]:
    subject = Path(setup["subject"])
    brief_path, brief_text, brief_hash = _brief(setup)
    prompt = (
        "Analyze this public Python repository as a prospective arctl research "
        "subject. Do not edit files or invent private evaluation data. The human "
        "ARCTL_SETUP.md below is authoritative intent; use repository evidence to "
        "complete it and identify only material missing, ambiguous, contradictory, "
        "or infeasible decisions. Return exactly one canonical field for every "
        "required id. A field source is 'setup brief' when explicitly supplied, "
        "'repository' when directly derived, and 'proposal' otherwise. Return at "
        "most one non-overlapping clarification in each group: target, trial_protocol, "
        "constraints, evaluator. Pairing, independence, seed selection, and hidden "
        "trials belong to the single trial_protocol clarification. Never ask the "
        "human to redefine a controller-owned rule supplied below. Ask only for the "
        "task-specific independent unit, outcome, fixed count or exact automatic "
        "calibration ladder/diagnostic/threshold, episode horizon, uncontrolled "
        "variation, enforceable resource limits with an explicit scope, and the "
        "evaluator-owned statistic and uncertainty method. Do not invent numeric "
        "limits, trial counts, confidence methods, distributions, or thresholds that "
        "lack brief or repository support; use null proposed_answer so the human must "
        "answer instead of accepting an invented default. Do not "
        "describe a process failure as a failed episode or assign it a score. Do not "
        "ask for a redundant negative-mean or tie rule when the controller decision "
        "mapping already covers it. Dependency installation is derived setup work, "
        "not a human question, unless the required dependency source is ambiguous. "
        "Policy and environment facts should normally be shown, not asked. Every "
        "field needs repository citations when available. Return only JSON.\n\n"
        + json.dumps(
            {
                "brief_path": str(brief_path),
                "brief_sha256": brief_hash,
                "brief": brief_text,
                "required_ids": QUESTION_IDS,
                "groups": QUESTION_GROUPS,
                "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    attempts = directory / "setup" / "discovery" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    if progress is not None:
        progress({"stage": "brief + repository discovery", "status": "started"})
    try:
        value = _agent_run(
            root=attempts / f"{attempt:04d}",
            worktree=subject,
            schema_value=discovery_schema(),
            output_name="discovery.public.json",
            prompt=prompt,
            writable_worktree=False,
            command_builder=command_builder,
            offline=offline,
        )
    except Exception:
        if progress is not None:
            progress({"stage": "brief + repository discovery", "status": "failed"})
        raise
    ids = [item["id"] for item in value["fields"]]
    if sorted(ids) != sorted(QUESTION_IDS) or len(ids) != len(set(ids)):
        raise StateError("setup discovery did not describe every required field once")
    if value["brief_sha256"] != brief_hash:
        raise StateError("setup discovery returned the wrong brief hash")
    seen_fields: set[str] = set()
    seen_groups: set[str] = set()
    for question in value["open_questions"]:
        group = question["id"]
        affected = set(question["affected_fields"])
        if (
            group in seen_groups
            or affected & seen_fields
            or not affected <= set(QUESTION_GROUPS[group])
        ):
            raise StateError("setup discovery produced overlapping clarifications")
        seen_groups.add(group)
        seen_fields.update(affected)
    atomic_write_json(directory / "setup" / "discovery.public.json", value)
    if progress is not None:
        progress({"stage": "brief + repository discovery", "status": "completed"})
    setup["state"] = "ANSWERS_REQUIRED"
    _save_setup(directory, setup)
    return value


def save_answers(
    directory: Path,
    setup: dict[str, Any],
    answers: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    discovery = json.loads(
        (directory / "setup" / "discovery.public.json").read_text(encoding="utf-8")
    )
    presentation = setup_presentation(discovery)
    required = {item["id"] for item in presentation["open_questions"]}
    if set(answers) == set(QUESTION_IDS):
        overrides = answers
        answers = {}
        required = set()
    if set(answers) != required or any(
        not isinstance(value, str) or not value.strip() for value in answers.values()
    ):
        raise ValidationError("setup answers must resolve every open clarification once")
    overrides = {} if overrides is None else overrides
    if not set(overrides) <= set(QUESTION_IDS) or any(
        not isinstance(value, str) or not value.strip() for value in overrides.values()
    ):
        raise ValidationError("setup overrides name unknown or empty canonical fields")
    normalized = {
        "schema_version": 2,
        "brief_sha256": discovery.get("brief_sha256"),
        "proposal": presentation["proposal"],
        "answers": {name: answers[name].strip() for name in sorted(answers)},
        "overrides": {name: overrides[name].strip() for name in sorted(overrides)},
    }
    atomic_write_json(
        directory / "setup" / "answers.public.json",
        normalized,
    )
    resolved = resolve_fields(
        presentation, normalized["answers"], normalized["overrides"]
    )
    atomic_write_json(
        directory / "setup" / "resolved.public.json",
        {
            "schema_version": 1,
            "brief_sha256": normalized["brief_sha256"],
            "fields": resolved,
            "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
        },
    )
    setup["state"] = "BUILD_REQUIRED"
    _save_setup(directory, setup)
    return normalized


def resolve_fields(
    presentation: Mapping[str, Any],
    answers: Mapping[str, str],
    overrides: Mapping[str, str],
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in presentation["proposal"]}
    resolved = []
    for identifier in QUESTION_IDS:
        item = by_id[identifier]
        answer = overrides.get(identifier)
        if answer is None:
            amendments = [
                answers[group]
                for group, members in QUESTION_GROUPS.items()
                if identifier in members and group in answers
            ]
            answer = item["proposed_answer"]
            if amendments and answer is None:
                answer = amendments[0]
            elif amendments and amendments[0] != answer:
                answer = f"{answer}\nHuman clarification: {amendments[0]}"
        resolved.append({**item, "resolved_answer": answer})
    return resolved


def _safe_files(
    root: Path,
    records: Sequence[Mapping[str, Any]],
    *,
    forbidden_roots: frozenset[str] = frozenset(),
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()
    for record in records:
        relative = Path(record["path"])
        if (
            relative.is_absolute()
            or relative == Path(".")
            or ".." in relative.parts
            or ".git" in relative.parts
            or (relative.parts and relative.parts[0] in forbidden_roots)
            or relative in seen
        ):
            raise StateError("setup agent proposed an unsafe file path")
        seen.add(relative)
        target = root / relative
        if target.exists() and target.is_symlink():
            raise StateError("setup agent proposed overwriting a symlink")
        atomic_write_text(target, record["content"])


def _remove_stale_generated_files(
    directory: Path,
    roots: Mapping[str, Path],
    current: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    current_paths = {
        (name, record["path"])
        for name, records in current.items()
        for record in records
    }
    attempts = directory / "setup" / "build" / "attempts"
    for output in attempts.glob("*/output/build.public.json"):
        try:
            value = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for name, root in roots.items():
            for record in value.get(name, []):
                relative = Path(record.get("path", ""))
                if (
                    not relative.parts
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or ".git" in relative.parts
                    or (name == "subject_files" and relative.parts[0] == "ARCTL_SETUP.md")
                    or (name == "evaluator_files" and relative.parts[0] == "private")
                    or (name, relative.as_posix()) in current_paths
                ):
                    continue
                target = root / relative
                if target.is_file() and not target.is_symlink():
                    target.unlink()


def _validate_build_contract(
    value: Mapping[str, Any], setup: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], TaskConfig]:
    try:
        Draft202012Validator(build_schema()).validate(value)
    except JsonSchemaError as error:
        raise ValidationError(f"build response contract: {error.message}") from error
    task = dict(value["task"])
    task.update(
        {
            "schema_version": 5,
            "task_id": setup["task_id"],
            "repo": setup["subject"],
            "evaluator": {
                "repo": setup["evaluator"],
                "commit": "SETUP_EVALUATOR_COMMIT",
            },
            "method": {
                "profile": "serial-v1",
                "allow_unverified_isolation": False,
            },
        }
    )
    errors = []
    try:
        task_config = TaskConfig.from_mapping(task)
    except ValidationError as error:
        errors.append(f"task contract: {error}")
        task_config = None
    manifest = dict(value["evaluator_manifest"])
    try:
        parsed_manifest = EvaluatorManifest.from_mapping(manifest)
        if parsed_manifest.schema_version != 3:
            raise ValidationError("manifest.schema_version must equal 3 for setup")
        if task_config is not None:
            parsed_manifest.validate_trial_setting(task_config.trials)
    except ValidationError as error:
        errors.append(f"evaluator manifest contract: {error}")
    serialized = json.dumps(task, sort_keys=True)
    missing = sorted(
        placeholder for placeholder in _PLACEHOLDERS if placeholder not in serialized
    )
    if missing:
        errors.append(f"task contract: missing commit placeholders {missing}")
    if any(
        record["path"] == "evaluator.manifest.json"
        for record in value["evaluator_files"]
    ):
        errors.append(
            "evaluator files: evaluator.manifest.json is controller-owned; "
            "use evaluator_manifest"
        )
    if errors:
        raise ValidationError("; ".join(errors))
    assert task_config is not None
    return task, manifest, task_config


def _public_files(root: Path, *, exclude_private: bool = False) -> tuple[Path, ...]:
    return tuple(
        path
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.relative_to(root).parts
        and (not exclude_private or "private" not in path.relative_to(root).parts)
    )


def _tree_hash(root: Path, *, exclude_private: bool = False) -> str:
    digest = hashlib.sha256()
    for path in _public_files(root, exclude_private=exclude_private):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_setup(
    directory: Path,
    setup: dict[str, Any],
    *,
    offline: bool,
    command_builder: SetupCommandBuilder | None = None,
    review_command_builder: SetupCommandBuilder | None = None,
    progress: SetupProgress | None = None,
) -> dict[str, Any]:
    subject = Path(setup["subject"])
    evaluator = Path(setup["evaluator"])
    environment = Path(setup["environment"])
    workspace = Path(setup["workspace"])
    branch = f"arctl/setup-{setup['task_id']}"
    current = _git(subject, "branch", "--show-current")
    if current != branch and not _clean_except_brief(subject):
        raise StateError("setup requires a clean subject Git worktree except ARCTL_SETUP.md")
    resolved_path = directory / "setup" / "resolved.public.json"
    if not resolved_path.is_file():
        answers = json.loads(
            (directory / "setup" / "answers.public.json").read_text(encoding="utf-8")
        )
        save_answers(
            directory,
            setup,
            answers.get("answers", {}),
            answers.get("overrides", {}),
        )
    requirements = json.loads(resolved_path.read_text(encoding="utf-8"))
    if current != branch:
        if _git(subject, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
            raise StateError(f"setup branch already exists: {branch}")
        _git(subject, "switch", "-q", "-c", branch)
    prior_readiness = directory / "setup" / "readiness.public.json"
    prior_findings = []
    if prior_readiness.is_file():
        prior_findings = json.loads(prior_readiness.read_text(encoding="utf-8")).get(
            "findings", []
        )
    attempts = directory / "setup" / "build" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
    prompt = (
        "Create a minimal faithful Python/uv arctl integration from the confirmed "
        "requirements. Return file contents; do not commit, access hidden data, or "
        "put secrets in output. Prefer existing project conventions. Generated "
        "subject adapters must load the checked-out candidate, expose JSON batch "
        "input/output, and keep infrastructure outside editable paths. Generate a "
        "manifest-v3 evaluator using paired mean, binary, or median reference logic "
        "when appropriate; otherwise label custom assumptions plainly. The task must "
        "include evaluator unittest coverage in evaluator/test_*.py for its public "
        "protocol, evidence shape, seed handling, and scoring behavior. The tests "
        "must not require private data. "
        "Return typed task-specific task fields and one complete manifest-v3 object; "
        "arctl owns their schema versions, task identity, subject/evaluator paths, "
        "method, and evaluator.manifest.json filename. The task still needs an "
        "environment reference using the named commit placeholders and a public_probe "
        "object with trial_equivalents. Treat controller_owned_contract below as "
        "fixed: do not turn operational "
        "failures into scored outcomes or replace the controller decision mapping. "
        "When a human clarification uses informal terminology, implement its explicit "
        "statistical quantity, unit, scope, and threshold only when unambiguous; "
        "otherwise leave a reviewable defect rather than silently guessing. Return "
        "only the required JSON.\n\n"
        + json.dumps(
            {
                "task_id": setup["task_id"],
                "subject": str(subject),
                "environment": str(environment),
                "evaluator": str(evaluator),
                "python": str(workspace / ".venv" / "bin" / "python"),
                "commit_placeholders": sorted(_PLACEHOLDERS),
                "requirements": requirements,
                "prior_review_findings": prior_findings,
                "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
                "typed_build_contract": SETUP_BUILD_CONTRACT,
                "completion_checklist": [
                    "task has every schema-v5 task-specific field",
                    "evaluator_manifest has every manifest-v3 field",
                    "operational failures produce no statistical score",
                    "public evaluator tests exercise evidence, seeds, and scoring",
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if progress is not None:
        progress({"stage": "generation", "status": "started"})
    try:
        value = _agent_run(
            root=attempts / f"{attempt:04d}",
            worktree=subject,
            schema_value=build_schema(),
            output_name="build.public.json",
            prompt=prompt,
            writable_worktree=False,
            read_paths=(environment, *_public_files(evaluator, exclude_private=True)),
            command_builder=command_builder,
            offline=offline,
            validate_output=False,
        )
    except Exception:
        if progress is not None:
            progress({"stage": "generation", "status": "failed"})
        raise
    if progress is not None:
        progress({"stage": "generation", "status": "completed"})
        progress({"stage": "task validation", "status": "started"})
    try:
        task_value, manifest_value, task = _validate_build_contract(value, setup)
    except ValidationError as first_error:
        if progress is not None:
            progress({"stage": "task validation", "status": "failed"})
            progress(
                {
                    "stage": "contract repair",
                    "status": "started",
                    "detail": "attempt 1/1",
                }
            )
        repair_attempt = attempt + 1
        repair_prompt = (
            "Repair the typed setup output. Return a complete replacement, not a patch. "
            "Correct every validator finding and recheck the supplied completion checklist. "
            "Do not change confirmed requirements or controller-owned rules. Return only JSON.\n\n"
            + json.dumps(
                {
                    "validator_findings": str(first_error),
                    "invalid_output": value,
                    "requirements": requirements,
                    "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
                    "typed_build_contract": SETUP_BUILD_CONTRACT,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        try:
            value = _agent_run(
                root=attempts / f"{repair_attempt:04d}",
                worktree=subject,
                schema_value=build_schema(),
                output_name="build.public.json",
                prompt=repair_prompt,
                writable_worktree=False,
                read_paths=(environment, *_public_files(evaluator, exclude_private=True)),
                command_builder=command_builder,
                offline=offline,
                validate_output=False,
            )
            task_value, manifest_value, task = _validate_build_contract(value, setup)
        except (StateError, ValidationError) as repair_error:
            if progress is not None:
                progress(
                    {
                        "stage": "contract repair",
                        "status": "failed",
                        "detail": "attempt 1/1",
                    }
                )
            raise StateError(
                "generated setup contract is invalid after one repair: "
                f"initial validation: {first_error}; repair: {repair_error}"
            ) from repair_error
        if progress is not None:
            progress(
                {
                    "stage": "contract repair",
                    "status": "completed",
                    "detail": "attempt 1/1",
                }
            )
            progress({"stage": "task validation", "status": "started"})
    if progress is not None:
        progress({"stage": "task validation", "status": "completed"})
    generated = {
        "subject_files": value["subject_files"],
        "environment_files": value["environment_files"],
        "evaluator_files": value["evaluator_files"],
    }
    _remove_stale_generated_files(
        directory,
        {
            "subject_files": subject,
            "environment_files": environment,
            "evaluator_files": evaluator,
        },
        generated,
    )
    _safe_files(
        subject,
        value["subject_files"],
        forbidden_roots=frozenset({"ARCTL_SETUP.md"}),
    )
    _safe_files(environment, value["environment_files"])
    _safe_files(
        evaluator,
        value["evaluator_files"],
        forbidden_roots=frozenset({"private"}),
    )
    import yaml

    atomic_write_text(
        directory / "task.draft.yaml", yaml.safe_dump(task_value, sort_keys=False)
    )
    atomic_write_json(evaluator / "evaluator.manifest.json", manifest_value)
    pyproject = (
        "[project]\nname = \"arctl-workspace-runtime\"\nversion = \"0.0.0\"\n"
        "requires-python = \">=3.11\"\ndependencies = "
        + json.dumps(value["dependencies"])
        + "\n"
    )
    atomic_write_text(workspace / "pyproject.toml", pyproject)
    uv = shutil.which("uv")
    if uv is None:
        raise StateError("uv is required for Python workspace setup")
    sync = [
        uv,
        "sync",
        "--project",
        str(workspace),
        "--no-install-project",
        *(("--offline",) if offline else ()),
    ]
    uv_environment = os.environ.copy()
    uv_environment["UV_CACHE_DIR"] = str(workspace / ".uv-cache")
    uv_environment["UV_PROJECT_ENVIRONMENT"] = str(workspace / ".venv")
    if progress is not None:
        progress({"stage": "dependencies", "status": "started"})
    completed = subprocess.run(
        sync,
        check=False,
        capture_output=True,
        text=True,
        env=uv_environment,
    )
    if completed.returncode:
        if progress is not None:
            progress({"stage": "dependencies", "status": "failed"})
        if offline:
            raise StateError("workspace dependencies are unavailable offline; rerun setup without --offline")
        raise StateError("uv failed to provision the workspace runtime: " + completed.stderr.strip())
    if progress is not None:
        progress({"stage": "dependencies", "status": "completed"})
        progress({"stage": "evaluator checks", "status": "started"})
    evaluator_tests = tuple(evaluator.glob("test_*.py"))
    if not evaluator_tests:
        raise StateError("generated evaluator must include public unittest coverage")
    evaluator_check = subprocess.run(
        [
            str(workspace / ".venv" / "bin" / "python"),
            "-m",
            "unittest",
            "discover",
            "-s",
            str(evaluator),
        ],
        cwd=evaluator,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if evaluator_check.returncode:
        detail = evaluator_check.stderr.strip() or evaluator_check.stdout.strip()
        raise StateError(f"generated evaluator conformance checks failed: {detail}")
    if progress is not None:
        progress({"stage": "evaluator checks", "status": "completed"})
    forbidden_command_roots = (evaluator.resolve(), directory.resolve())
    for command in (*task.public_checks, task.public_probe):
        for argument in command:
            for forbidden in forbidden_command_roots:
                if str(forbidden) in argument:
                    raise StateError("generated public command accesses evaluator or task storage")
    if progress is not None:
        progress({"stage": "public checks", "status": "started"})
    for label, command in (
        *(('public check', item) for item in task.public_checks),
        ("public probe", task.public_probe),
    ):
        completed = subprocess.run(
            command,
            cwd=subject,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise StateError(f"generated {label} failed: {detail}")
    if progress is not None:
        progress({"stage": "public checks", "status": "completed"})
    review_prompt = (
        "Review this generated arctl setup. Inspect the subject integration, public "
        "environment, evaluator code, manifest, task draft, and confirmed requirements. "
        "Report every concrete leakage, trial-independence, statistical-contract, "
        "telemetry, seed-handling, runtime, or fidelity defect. Do not read evaluator/private. "
        "Reject any generated protocol that contradicts the supplied controller-owned "
        "contract, silently resolves an ambiguous human statistic or timeout scope, "
        "claims an unenforced resource limit, or scores an operational failure. An "
        "empty findings array means no supported defect was found. Return only JSON.\n\n"
        + json.dumps(
            {
                "requirements": requirements,
                "task_draft": str(directory / "task.draft.yaml"),
                "subject": str(subject),
                "environment": str(environment),
                "evaluator": str(evaluator),
                "controller_owned_contract": SETUP_CONTROLLER_CONTRACT,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    review_attempts = directory / "setup" / "review" / "attempts"
    review_attempt = (
        1 + len(tuple(review_attempts.glob("*"))) if review_attempts.is_dir() else 1
    )
    if progress is not None:
        progress({"stage": "setup review", "status": "started"})
    try:
        review = _agent_run(
            root=review_attempts / f"{review_attempt:04d}",
            worktree=subject,
            schema_value=review_schema(),
            output_name="review.public.json",
            prompt=review_prompt,
            writable_worktree=False,
            read_paths=(
                subject,
                environment,
                directory,
                *_public_files(evaluator, exclude_private=True),
            ),
            command_builder=review_command_builder,
            offline=offline,
        )
    except Exception:
        if progress is not None:
            progress({"stage": "setup review", "status": "failed"})
        raise
    if progress is not None:
        progress({"stage": "setup review", "status": "completed"})
    readiness = {
        "schema_version": 1,
        "requirements": "ready",
        "subject": "ready" if value["subject_files"] or _clean(subject) else "blocked",
        "environment": "ready",
        "evaluator": "ready",
        "runtime": "ready",
        "dependencies": value["dependencies"],
        "review": "ready" if not review["findings"] else "blocked",
        "findings": review["findings"],
        "tree_hashes": {
            "subject": _tree_hash(subject),
            "environment": _tree_hash(environment),
            "evaluator": _tree_hash(evaluator, exclude_private=True),
        },
    }
    encoded = json.dumps(
        {"build": value, "review": review, "readiness": readiness},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    token = hashlib.sha256(encoded).hexdigest()[:16]
    readiness["acceptance_token"] = token
    atomic_write_json(directory / "setup" / "readiness.public.json", readiness)
    setup["state"] = "READY_FOR_SETUP_ACCEPTANCE" if not review["findings"] else "REVIEW_FAILED"
    setup["acceptance_token"] = token
    _save_setup(directory, setup)
    return readiness


def accept_setup(directory: Path, setup: dict[str, Any], token: str) -> TaskConfig:
    if setup.get("state") != "READY_FOR_SETUP_ACCEPTANCE":
        raise StateError("setup is not ready for acceptance")
    if token != setup.get("acceptance_token"):
        raise StateError("setup acceptance token does not match")
    subject = Path(setup["subject"])
    environment = Path(setup["environment"])
    evaluator = Path(setup["evaluator"])
    readiness = json.loads(
        (directory / "setup" / "readiness.public.json").read_text(encoding="utf-8")
    )
    current_hashes = {
        "subject": _tree_hash(subject),
        "environment": _tree_hash(environment),
        "evaluator": _tree_hash(evaluator, exclude_private=True),
    }
    if readiness.get("tree_hashes") != current_hashes:
        raise StateError("generated setup trees changed after review")
    subject_commit = _commit(subject, f"Prepare {setup['task_id']} for arctl")
    environment_commit = _commit(environment, f"Define {setup['task_id']} environment")
    evaluator_commit = _commit(evaluator, f"Define {setup['task_id']} evaluator")
    text = (directory / "task.draft.yaml").read_text(encoding="utf-8")
    replacements = {
        "SETUP_SUBJECT_COMMIT": subject_commit,
        "SETUP_ENVIRONMENT_COMMIT": environment_commit,
        "SETUP_EVALUATOR_COMMIT": evaluator_commit,
    }
    for placeholder, commit in replacements.items():
        text = text.replace(placeholder, commit)
    if any(placeholder in text for placeholder in _PLACEHOLDERS):
        raise StateError("generated task contains unresolved commit placeholders")
    atomic_write_text(directory / "task.yaml", text)
    task = load_task(directory / "task.yaml")
    if task.evaluator.commit != evaluator_commit:
        raise StateError("accepted task does not lock the generated evaluator")
    environment_locks = {
        (source.path.resolve(), source.commit)
        for source in task.environment_sources
        if source.path is not None
    }
    if (environment.resolve(), environment_commit) not in environment_locks:
        raise StateError("accepted task does not lock the generated environment")
    if (subject.resolve(), subject_commit) not in environment_locks:
        raise StateError("accepted task does not lock the subject interface source")
    setup["state"] = "READY_FOR_APPROVAL"
    requirements = json.loads(
        (directory / "setup" / "answers.public.json").read_text(encoding="utf-8")
    )
    if requirements.get("schema_version") == 2:
        proposed = {
            item["id"]: item["proposed_answer"] for item in requirements["proposal"]
        }
        setup["evaluator_pattern"] = requirements["overrides"].get(
            "evaluator_pattern",
            requirements["answers"].get("evaluator", proposed["evaluator_pattern"]),
        )
    else:
        setup["evaluator_pattern"] = requirements["answers"]["evaluator_pattern"]
    setup["subject_commit"] = subject_commit
    setup["environment_commit"] = environment_commit
    setup["evaluator_commit"] = evaluator_commit
    _save_setup(directory, setup)
    return task
