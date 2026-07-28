"""Human-confirmed locking of a task and external evaluator revision."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    confirmation_token: str
    manifest: EvaluatorManifest


def preview_approval(task_file: Path, task: TaskConfig) -> ApprovalPreview:
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
    manifest.validate_trial_setting(task.trials)
    task_hash = hashlib.sha256(task_file.read_bytes()).hexdigest()
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
    token = hashlib.sha256(
        b"\0".join(
            (
                b"arctl-approval-v1",
                task_hash.encode(),
                commit.encode(),
                manifest_hash.encode(),
            )
        )
    ).hexdigest()[:16]
    return ApprovalPreview(
        task_id=task.task_id,
        task_hash=task_hash,
        evaluator_commit=commit,
        manifest_hash=manifest_hash,
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
            "schema_version": 1,
            "task_sha256": preview.task_hash,
            "evaluator_commit": preview.evaluator_commit,
            "manifest_sha256": preview.manifest_hash,
            "champion": champion,
        },
    )
    os.chmod(task_directory / "task.yaml", 0o444)


def verify_approval(task_directory: Path, task: TaskConfig) -> dict[str, str]:
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
    }
    if not isinstance(approval, dict) or set(approval) != expected_fields:
        raise StateError("task approval record is invalid")
    if (
        approval["schema_version"] != 1
        or approval["task_sha256"] != task_hash
        or approval["manifest_sha256"] != manifest_hash
        or approval["evaluator_commit"] != evaluator_commit
        or evaluator_commit != resolve_commit(task.evaluator.repo, task.evaluator.commit)
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
        "champion": resolve_commit(task.repo, champion_ref),
    }
