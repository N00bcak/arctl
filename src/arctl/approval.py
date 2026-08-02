"""Human-confirmed locking of a task and external evaluator revision."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Mapping

from .errors import StateError, ValidationError
from .git import ensure_clean_worktree, resolve_commit
from .manifest import EvaluatorManifest
from .models import TaskConfig
from .storage import atomic_write_bytes, atomic_write_json, atomic_write_text
from .trials import freeze_fixed_trial_count, load_trial_count

_MANIFEST_PATH = "evaluator.manifest.json"


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip()
        raise StateError(f"cannot read evaluator revision: {detail}")
    return completed.stdout


@dataclass(frozen=True)
class ApprovalPreview:
    task_id: str
    task_hash: str
    evaluator_commit: str
    manifest_hash: str
    environment_hashes: Mapping[str, str]
    confirmation_token: str
    manifest: EvaluatorManifest


def _source_files(task: TaskConfig, source_id: str) -> tuple[tuple[str, Path], ...]:
    source = next(item for item in task.environment_sources if item.identifier == source_id)
    if source.path is None:
        return ()
    root = source.path
    if root.is_symlink():
        raise ValidationError(f"environment source is a symlink: {source.identifier}")
    if root.is_file():
        if source.include:
            raise ValidationError(
                f"file environment source must not declare include patterns: {source.identifier}"
            )
        paths = (root,)
    elif root.is_dir():
        if not source.include:
            raise ValidationError(
                f"directory environment source requires include patterns: {source.identifier}"
            )
        paths = tuple(
            sorted(
                {
                    match
                    for pattern in source.include
                    for match in root.glob(pattern)
                    if match.is_file()
                }
            )
        )
    else:
        raise ValidationError(f"environment source does not exist: {source.identifier}")
    if not paths:
        raise ValidationError(f"environment source matched no files: {source.identifier}")
    for path in paths:
        if path.is_symlink():
            raise ValidationError(
                f"environment source contains a symlink: {source.identifier}"
            )
    base = root if root.is_dir() else root.parent
    return tuple((path.relative_to(base).as_posix(), path) for path in paths)


def environment_source_hashes(
    task: TaskConfig,
    *,
    task_directory: Path,
) -> dict[str, str]:
    repo = task.repo.resolve()
    evaluator = task.evaluator.repo.resolve()
    private = task_directory.resolve()
    hashes: dict[str, str] = {}
    for source in task.environment_sources:
        digest = hashlib.sha256()
        if source.path is None:
            digest.update(
                json.dumps(
                    {
                        "command": source.command,
                        "backed_by": source.backed_by,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            hashes[source.identifier] = digest.hexdigest()
            continue
        resolved = source.path.resolve()
        if resolved == evaluator or evaluator in resolved.parents:
            raise ValidationError("environment sources must not read the evaluator repo")
        if resolved == private or private in resolved.parents:
            raise ValidationError("environment sources must not read controller task data")
        for relative, path in _source_files(task, source.identifier):
            resolved_file = path.resolve()
            if resolved_file == evaluator or evaluator in resolved_file.parents:
                raise ValidationError("environment sources must not read the evaluator repo")
            if resolved_file == private or private in resolved_file.parents:
                raise ValidationError("environment sources must not read controller task data")
            try:
                repo_path = resolved_file.relative_to(repo).as_posix()
            except ValueError:
                pass
            else:
                if any(fnmatchcase(repo_path, pattern) for pattern in task.editable_paths):
                    raise ValidationError(
                        f"environment source overlaps editable path: {repo_path}"
                    )
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        hashes[source.identifier] = digest.hexdigest()
    return hashes


def preview_approval(task_file: Path, task: TaskConfig) -> ApprovalPreview:
    if task.schema_version != 3:
        raise ValidationError("task must use schema v3; create and approve a new task")
    target = task.repo.resolve()
    evaluator = task.evaluator.repo.resolve()
    if evaluator == target or target in evaluator.parents or evaluator in target.parents:
        raise ValidationError("evaluator repo must be outside the target repo")
    if not (evaluator / ".git").exists():
        raise ValidationError("evaluator repo must be a Git repository")
    commit = resolve_commit(evaluator, task.evaluator.commit)
    raw_manifest = _git_bytes(evaluator, "show", f"{commit}:{_MANIFEST_PATH}")
    try:
        value: Any = json.loads(raw_manifest)
    except json.JSONDecodeError as error:
        raise ValidationError("approved evaluator manifest is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValidationError("approved evaluator manifest must contain one object")
    manifest = EvaluatorManifest.from_mapping(value)
    if manifest.schema_version != 3:
        raise ValidationError("new tasks require a manifest-v3 telemetry contract")
    if manifest.subject_visible_seed:
        raise ValidationError("new tasks require evaluator-hidden trial seeds")
    manifest.validate_trial_setting(task.trials)
    if task.trials == "auto" and not manifest.calibration.controller_pilot:
        raise ValidationError(
            "new automatic tasks require a controller-run pilot"
        )
    task_hash = hashlib.sha256(task_file.read_bytes()).hexdigest()
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
    try:
        environment_hashes = environment_source_hashes(
            task, task_directory=task_file.parent
        )
    except OSError as error:
        raise ValidationError("environment source cannot be read") from error
    token = hashlib.sha256(
        b"\0".join(
            (
                b"arctl-approval-v2",
                task_hash.encode(),
                commit.encode(),
                manifest_hash.encode(),
                json.dumps(environment_hashes, sort_keys=True).encode(),
            )
        )
    ).hexdigest()[:16]
    return ApprovalPreview(
        task_id=task.task_id,
        task_hash=task_hash,
        evaluator_commit=commit,
        manifest_hash=manifest_hash,
        environment_hashes=environment_hashes,
        confirmation_token=token,
        manifest=manifest,
    )


def confirm_approval(
    task_directory: Path,
    task: TaskConfig,
    preview: ApprovalPreview,
    confirmation_token: str,
) -> None:
    approval_path = task_directory / "approval.json"
    if approval_path.exists():
        raise StateError("task is already approved; changes require a new task")
    current = preview_approval(task_directory / "task.yaml", task)
    if current != preview:
        raise StateError("task or evaluator changed after approval was presented")
    if confirmation_token != preview.confirmation_token:
        raise StateError("approval confirmation token does not match")

    raw_manifest = _git_bytes(
        task.evaluator.repo,
        "show",
        f"{preview.evaluator_commit}:{_MANIFEST_PATH}",
    )
    atomic_write_text(task_directory / "evaluator.commit", preview.evaluator_commit + "\n")
    atomic_write_bytes(task_directory / "evaluator.manifest.json", raw_manifest)

    champion_ref = f"refs/arctl/{task.task_id}/champion"
    ensure_clean_worktree(task.repo)
    champion = resolve_commit(task.repo, "HEAD")
    existing = subprocess.run(
        ["git", "-C", str(task.repo), "rev-parse", "--verify", "--quiet", champion_ref],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if existing and existing != champion:
        raise StateError("champion ref already exists at a different commit")
    if not existing:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(task.repo),
                "update-ref",
                champion_ref,
                champion,
                "0" * 40,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise StateError("could not initialize champion ref")

    freeze_fixed_trial_count(task_directory, task)
    atomic_write_json(
        approval_path,
        {
            "schema_version": 2,
            "task_sha256": preview.task_hash,
            "evaluator_commit": preview.evaluator_commit,
            "manifest_sha256": preview.manifest_hash,
            "champion": champion,
            "environment_sha256": dict(preview.environment_hashes),
        },
    )
    os.chmod(task_directory / "task.yaml", 0o444)


def verify_approval(task_directory: Path, task: TaskConfig) -> dict[str, str]:
    if task.schema_version != 3:
        raise StateError("task must use schema v3; create and approve a new task")
    try:
        approval = json.loads((task_directory / "approval.json").read_text())
        task_hash = hashlib.sha256((task_directory / "task.yaml").read_bytes()).hexdigest()
        manifest_hash = hashlib.sha256(
            (task_directory / "evaluator.manifest.json").read_bytes()
        ).hexdigest()
        evaluator_commit = (task_directory / "evaluator.commit").read_text().strip()
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("task approval is incomplete or unreadable") from error
    expected_fields = {
        "schema_version",
        "task_sha256",
        "evaluator_commit",
        "manifest_sha256",
        "champion",
        "environment_sha256",
    }
    if not isinstance(approval, dict) or set(approval) != expected_fields:
        raise StateError("task approval record is invalid")
    try:
        environment_hashes = environment_source_hashes(
            task, task_directory=task_directory
        )
    except (OSError, ValidationError) as error:
        raise StateError("approved environment sources are invalid") from error
    if (
        approval["schema_version"] != 2
        or approval["task_sha256"] != task_hash
        or approval["manifest_sha256"] != manifest_hash
        or approval["evaluator_commit"] != evaluator_commit
        or evaluator_commit != resolve_commit(task.evaluator.repo, task.evaluator.commit)
        or approval["environment_sha256"]
        != environment_hashes
    ):
        raise StateError("approved task or evaluator artifacts changed")
    champion_ref = f"refs/arctl/{task.task_id}/champion"
    if task.trials != "auto":
        load_trial_count(task_directory, task)
    if resolve_commit(task.repo, champion_ref) != approval["champion"]:
        # A promoted champion is expected; only require that the ref remains valid.
        resolve_commit(task.repo, champion_ref)
    return {
        "task_sha256": task_hash,
        "manifest_sha256": manifest_hash,
        "evaluator_commit": evaluator_commit,
        "approved_champion": approval["champion"],
        "champion": resolve_commit(task.repo, champion_ref),
    }
