"""Version-scoped Python bytecode caches for immutable experiments."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .errors import StateError
from .storage import atomic_write_json


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _python_identity(executable: str) -> dict[str, str]:
    completed = subprocess.run(
        [
            executable,
            "-I",
            "-c",
            (
                "import json,platform,sys;"
                "print(json.dumps({'cache_tag':sys.implementation.cache_tag,"
                "'implementation':platform.python_implementation(),"
                "'version':platform.python_version()}))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise StateError("approved Python runtime identity is unavailable") from error
    if (
        completed.returncode
        or not isinstance(value, Mapping)
        or set(value) != {"cache_tag", "implementation", "version"}
        or any(not isinstance(item, str) or not item for item in value.values())
    ):
        raise StateError("approved Python runtime identity is unavailable")
    return dict(value)


def _resolved_executable(executable: str) -> Path:
    path = Path(executable)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        located = shutil.which(executable)
        if located is None:
            raise StateError("approved Python runtime identity is unavailable")
        resolved = Path(located).resolve()
    if not resolved.is_file():
        raise StateError("approved Python runtime identity is unavailable")
    return resolved


def ensure_experiment_bytecode_cache(
    task_directory: Path,
    experiment: Path,
    *,
    python_executable: str,
) -> Path:
    """Create or validate one bytecode namespace for an experiment version."""
    task_directory = task_directory.resolve()
    if (
        experiment.is_symlink()
        or experiment.parent.resolve() != task_directory / "experiments"
        or not experiment.name.isdigit()
    ):
        raise StateError("experiment bytecode cache scope is invalid")
    experiment = experiment.resolve()
    request = experiment / "request.public.json"
    approval = task_directory / "approval.json"
    manifest = task_directory / "evaluator.manifest.json"
    try:
        request_bytes = request.read_bytes()
        approval_bytes = approval.read_bytes()
        manifest_bytes = manifest.read_bytes()
    except OSError as error:
        raise StateError("experiment bytecode cache provenance is incomplete") from error
    executable = _resolved_executable(python_executable)
    identity = _python_identity(str(executable))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", identity["cache_tag"]):
        raise StateError("approved Python cache tag is invalid")
    runtime = experiment / "runtime"
    bytecode_root = runtime / "python-bytecode"
    for directory in (runtime, bytecode_root):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise StateError("experiment bytecode cache path is invalid")
        directory.mkdir(parents=True, exist_ok=True)
    root = bytecode_root / identity["cache_tag"]
    relative = root.relative_to(task_directory).as_posix()
    expected: dict[str, Any] = {
        "experiment_id": int(experiment.name),
        "request_sha256": _sha256(request_bytes),
        "approval_sha256": _sha256(approval_bytes),
        "runtime_manifest_sha256": _sha256(manifest_bytes),
        "python": {**identity, "executable": str(executable)},
        "cache_path": relative,
    }
    record = experiment / "runtime" / "bytecode-cache.private.json"
    if record.is_file():
        try:
            saved = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("experiment bytecode cache manifest is invalid") from error
        if saved != expected:
            raise StateError("experiment bytecode cache provenance changed")
    elif record.exists() or record.is_symlink():
        raise StateError("experiment bytecode cache manifest is invalid")
    else:
        atomic_write_json(record, expected)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise StateError("experiment bytecode cache path is invalid")
    root.mkdir(parents=True, exist_ok=True)
    return root


def mirrored_source_root(cache: Path, source_root: Path) -> Path:
    resolved = source_root.resolve()
    parts = resolved.parts[1:] if resolved.is_absolute() else resolved.parts
    return cache.joinpath(*parts)


def invalidate_worktree_bytecode(cache: Path, source_root: Path) -> None:
    """Remove only bytecode whose source path is inside one mutable worktree."""
    target = mirrored_source_root(cache.resolve(), source_root)
    if target == cache.resolve() or cache.resolve() not in target.parents:
        raise StateError("worktree bytecode path escapes its cache")
    if target.is_symlink():
        raise StateError("worktree bytecode path is a symlink")
    if not target.exists():
        return
    import shutil

    shutil.rmtree(target)
