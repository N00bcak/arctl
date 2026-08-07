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
            "prompt": text,
            "why": text,
            "proposed_answer": text,
            "citations": {"type": "array", "items": citation},
        }
    )
    return _schema(
        {
            "schema_version": {"type": "integer", "const": 1},
            "summary": text,
            "questions": {
                "type": "array",
                "minItems": len(QUESTION_IDS),
                "maxItems": len(QUESTION_IDS),
                "items": question,
            },
        }
    )


def build_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    file_record = _schema({"path": text, "content": {"type": "string"}})
    files = {"type": "array", "items": file_record}
    return _schema(
        {
            "schema_version": {"type": "integer", "const": 1},
            "summary": text,
            "dependencies": {"type": "array", "items": text},
            "subject_files": files,
            "environment_files": files,
            "evaluator_files": files,
            "task_yaml": text,
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
        Draft202012Validator(schema_value).validate(value)
    except (OSError, json.JSONDecodeError, JsonSchemaError) as error:
        raise StateError(f"setup agent wrote invalid {output_name}") from error
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
    if not _clean(subject):
        raise StateError("setup requires a clean subject Git worktree")
    evaluator = workspace / "evaluator"
    environment = workspace / "environment"
    evaluator.mkdir()
    environment.mkdir()
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
) -> dict[str, Any]:
    subject = Path(setup["subject"])
    prompt = (
        "Analyze this public Python repository as a prospective arctl research "
        "subject. Do not edit files or invent private evaluation data. Produce "
        "exactly one question for every required id, with the best proposed answer "
        "supported by repository citations. Distinguish the policy to improve from "
        "the environment it acts in. Treat statistical independence, hidden data, "
        "and evaluator choice as proposals requiring human confirmation. Return only "
        "the required JSON. Required ids: "
        + ", ".join(QUESTION_IDS)
    )
    attempts = directory / "setup" / "discovery" / "attempts"
    attempt = 1 + len(tuple(attempts.glob("*"))) if attempts.is_dir() else 1
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
    ids = [item["id"] for item in value["questions"]]
    if sorted(ids) != sorted(QUESTION_IDS) or len(ids) != len(set(ids)):
        raise StateError("setup discovery did not answer every required question once")
    atomic_write_json(directory / "setup" / "discovery.public.json", value)
    setup["state"] = "ANSWERS_REQUIRED"
    _save_setup(directory, setup)
    return value


def save_answers(
    directory: Path,
    setup: dict[str, Any],
    answers: Mapping[str, Any],
) -> dict[str, str]:
    if set(answers) != set(QUESTION_IDS) or any(
        not isinstance(value, str) or not value.strip() for value in answers.values()
    ):
        raise ValidationError("setup answers must provide every required question")
    normalized = {name: answers[name].strip() for name in QUESTION_IDS}
    atomic_write_json(
        directory / "setup" / "answers.public.json",
        {"schema_version": 1, "answers": normalized},
    )
    setup["state"] = "BUILD_REQUIRED"
    _save_setup(directory, setup)
    return normalized


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


def _task_value(path: Path) -> TaskConfig:
    return load_task(path)


def build_setup(
    directory: Path,
    setup: dict[str, Any],
    *,
    offline: bool,
    command_builder: SetupCommandBuilder | None = None,
    review_command_builder: SetupCommandBuilder | None = None,
) -> dict[str, Any]:
    subject = Path(setup["subject"])
    evaluator = Path(setup["evaluator"])
    environment = Path(setup["environment"])
    workspace = Path(setup["workspace"])
    branch = f"arctl/setup-{setup['task_id']}"
    current = _git(subject, "branch", "--show-current")
    if current != branch and not _clean(subject):
        raise StateError("setup requires a clean subject Git worktree")
    answers = json.loads(
        (directory / "setup" / "answers.public.json").read_text(encoding="utf-8")
    )["answers"]
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
        "must not require private data. The task must "
        "use schema_version 5, a public_probe object with trial_equivalents, absolute "
        "paths supplied below, method serial-v1, and commit placeholders exactly as "
        "named. Return only the required JSON.\n\n"
        + json.dumps(
            {
                "task_id": setup["task_id"],
                "subject": str(subject),
                "environment": str(environment),
                "evaluator": str(evaluator),
                "python": str(workspace / ".venv" / "bin" / "python"),
                "commit_placeholders": sorted(_PLACEHOLDERS),
                "answers": answers,
                "prior_review_findings": prior_findings,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
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
    )
    _safe_files(subject, value["subject_files"])
    _safe_files(environment, value["environment_files"])
    _safe_files(
        evaluator,
        value["evaluator_files"],
        forbidden_roots=frozenset({"private"}),
    )
    if any(placeholder not in value["task_yaml"] for placeholder in _PLACEHOLDERS):
        raise StateError("generated task omits required commit placeholders")
    atomic_write_text(directory / "task.draft.yaml", value["task_yaml"])
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
    completed = subprocess.run(
        sync,
        check=False,
        capture_output=True,
        text=True,
        env=uv_environment,
    )
    if completed.returncode:
        if offline:
            raise StateError("workspace dependencies are unavailable offline; rerun setup without --offline")
        raise StateError("uv failed to provision the workspace runtime: " + completed.stderr.strip())
    manifest_path = evaluator / "evaluator.manifest.json"
    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        EvaluatorManifest.from_mapping(manifest_value)
        task = _task_value(directory / "task.draft.yaml")
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise StateError("generated task or evaluator manifest is invalid") from error
    if task.schema_version != 5 or task.repo.resolve() != subject.resolve():
        raise StateError("generated task does not describe this schema-v5 workspace")
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
    forbidden_command_roots = (evaluator.resolve(), directory.resolve())
    for command in (*task.public_checks, task.public_probe):
        for argument in command:
            for forbidden in forbidden_command_roots:
                if str(forbidden) in argument:
                    raise StateError("generated public command accesses evaluator or task storage")
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
    review_prompt = (
        "Review this generated arctl setup. Inspect the subject integration, public "
        "environment, evaluator code, manifest, task draft, and confirmed requirements. "
        "Report every concrete leakage, trial-independence, statistical-contract, "
        "telemetry, seed-handling, runtime, or fidelity defect. Do not read evaluator/private. "
        "An empty findings array means no supported defect was found. Return only JSON.\n\n"
        + json.dumps(
            {
                "answers": answers,
                "task_draft": str(directory / "task.draft.yaml"),
                "subject": str(subject),
                "environment": str(environment),
                "evaluator": str(evaluator),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    review_attempts = directory / "setup" / "review" / "attempts"
    review_attempt = (
        1 + len(tuple(review_attempts.glob("*"))) if review_attempts.is_dir() else 1
    )
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
    setup["evaluator_pattern"] = json.loads(
        (directory / "setup" / "answers.public.json").read_text(encoding="utf-8")
    )["answers"]["evaluator_pattern"]
    setup["subject_commit"] = subject_commit
    setup["environment_commit"] = environment_commit
    setup["evaluator_commit"] = evaluator_commit
    _save_setup(directory, setup)
    return task
