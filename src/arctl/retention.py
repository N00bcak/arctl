"""Lifecycle-aware, crash-safe cleanup of disposable task artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sysconfig
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import StateError
from .process import read_valid_result, recorded_process_is_live
from .storage import atomic_write_bytes, atomic_write_json
from .taskio import load_task

_GC_SCHEMA_VERSION = 1
_ROOT_CANONICAL = frozenset(
    {
        "approval.json", "backend-approval.json", "evaluator.commit",
        "evaluator.manifest.json", "method.lock.json", "setup.json",
        "task.draft.yaml", "task.yaml", "trial-count.json",
    }
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _relative(root: Path, path: Path) -> str:
    if not _inside(root, path):
        raise StateError("retention path escapes the task root")
    return path.relative_to(root).as_posix()


def _lstat_identity(path: Path) -> dict[str, Any]:
    value = path.lstat()
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": stat.S_IMODE(value.st_mode),
        "type": (
            "directory" if stat.S_ISDIR(value.st_mode) else
            "file" if stat.S_ISREG(value.st_mode) else
            "symlink" if stat.S_ISLNK(value.st_mode) else "other"
        ),
        "size": value.st_size,
    }


def _stable_identity(path: Path) -> dict[str, Any]:
    identity = _lstat_identity(path)
    return {key: identity[key] for key in ("device", "inode", "type")}


def _tree_inventory(
    path: Path, *, allow_symlinks: bool = False
) -> tuple[int, int, str]:
    """Return bytes, path count, and a metadata digest without reading disposable data."""
    digest = hashlib.sha256()
    total = 0
    count = 0
    for candidate in (path, *sorted(path.rglob("*"))):
        relative = "." if candidate == path else candidate.relative_to(path).as_posix()
        identity = _lstat_identity(candidate)
        if identity["type"] == "symlink" and not allow_symlinks:
            raise StateError(f"disposable root contains a symlink: {candidate}")
        if identity["device"] != path.lstat().st_dev:
            raise StateError(f"disposable root crosses a filesystem boundary: {candidate}")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(_json_bytes(identity))
        if identity["type"] == "symlink":
            digest.update(os.readlink(candidate).encode())
        digest.update(b"\0")
        count += 1
        if identity["type"] == "file":
            total += identity["size"]
    return total, count, digest.hexdigest()


def _synthetic_scratch_root(path: Path) -> bool:
    if not re.fullmatch(r"codex-arg[0-9A-Za-z]+", path.name) or not path.is_dir():
        return False
    expected = {".lock", "applypatch", "apply_patch", "codex-execve-wrapper"}
    try:
        children = {item.name: item for item in path.iterdir()}
        return (
            set(children) == expected
            and children[".lock"].is_file()
            and not children[".lock"].is_symlink()
            and children[".lock"].stat().st_size == 0
            and all(
                children[name].is_symlink()
                and Path(os.readlink(children[name])).name == "codex"
                for name in expected - {".lock"}
            )
        )
    except OSError:
        return False


def _canonical_paths(task: Path) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for name in _ROOT_CANONICAL:
        path = task / name
        if path.exists() and not path.is_symlink():
            paths.add(path)
    for relative in ("strategy", "exploration", "reports"):
        root = task / relative
        if root.is_dir() and not root.is_symlink():
            paths.update(path for path in root.rglob("*") if not path.is_symlink())
    setup = task / "setup"
    if setup.is_dir() and not setup.is_symlink():
        for directory, names, filenames in os.walk(setup, topdown=True):
            parent = Path(directory)
            names[:] = sorted(
                name
                for name in names
                if name
                not in {
                    "staging",
                    "pycache",
                    "__pycache__",
                    "home",
                    "codex-home",
                    "sandbox-home",
                }
                and not name.startswith("codex-arg")
                and not (parent / name).is_symlink()
            )
            paths.update(
                parent / name
                for name in sorted(filenames)
                if not (parent / name).is_symlink()
            )
    experiments = task / "experiments"
    if experiments.is_dir():
        for experiment in experiments.iterdir():
            if not experiment.is_dir() or not experiment.name.isdigit():
                continue
            for path in experiment.iterdir():
                if path.is_file() and path.name not in {".DS_Store"}:
                    paths.add(path)
            for relative in (
                "comparisons/primary/evidence.private.json",
                "comparisons/primary/reservation.private.json",
                "comparisons/primary/outputs/candidate/result.json",
                "comparisons/primary/outputs/champion/result.json",
                "comparisons/primary/outputs/prepare/batch.public.json",
                "comparisons/primary/outputs/prepare/response.json",
                "comparisons/primary/outputs/prepare/scoring.private.json",
                "comparisons/primary/outputs/score/evidence.json",
            ):
                path = experiment / relative
                if path.exists() and not path.is_symlink():
                    paths.add(path)
    return tuple(sorted(paths))


def canonical_snapshot(task: Path, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
    root = task.resolve()
    excluded = frozenset(exclude)
    entries: list[dict[str, Any]] = []
    for path in _canonical_paths(root):
        relative = _relative(root, path)
        if any(relative == item or relative.startswith(item + "/") for item in excluded):
            continue
        identity = _lstat_identity(path)
        entry = {
            "path": relative,
            "type": identity["type"],
            "mode": identity["mode"],
            "size": identity["size"],
        }
        if identity["type"] == "file":
            entry["sha256"] = _sha256(path.read_bytes())
        entries.append(entry)
    return {"schema_version": 1, "entries": entries, "sha256": _sha256(_json_bytes(entries))}


def experiment_canonical_snapshot(task: Path, experiment: Path) -> dict[str, Any]:
    root = task.resolve()
    experiment = experiment.resolve()
    if experiment.parent != root / "experiments" or not experiment.name.isdigit():
        raise StateError("experiment cleanup scope is invalid")
    paths: set[Path] = {
        path
        for path in experiment.iterdir()
        if path.is_file() and path.name != ".DS_Store" and not path.is_symlink()
    }
    for relative in (
        "comparisons/primary/evidence.private.json",
        "comparisons/primary/reservation.private.json",
        "comparisons/primary/outputs/candidate/result.json",
        "comparisons/primary/outputs/champion/result.json",
        "comparisons/primary/outputs/prepare/batch.public.json",
        "comparisons/primary/outputs/prepare/response.json",
        "comparisons/primary/outputs/prepare/scoring.private.json",
        "comparisons/primary/outputs/score/evidence.json",
    ):
        path = experiment / relative
        if path.exists() and not path.is_symlink():
            paths.add(path)
    entries: list[dict[str, Any]] = []
    for path in sorted(paths):
        identity = _lstat_identity(path)
        entry = {
            "path": _relative(root, path),
            "type": identity["type"],
            "mode": identity["mode"],
            "size": identity["size"],
        }
        if identity["type"] == "file":
            entry["sha256"] = _sha256(path.read_bytes())
        entries.append(entry)
    return {
        "schema_version": 1,
        "entries": entries,
        "sha256": _sha256(_json_bytes(entries)),
    }


def _process_terminal(process_directory: Path, task: Path) -> bool:
    try:
        read_valid_result(process_directory)
        return True
    except StateError:
        pass
    parts = process_directory.relative_to(task).parts
    if "experiments" in parts:
        index = parts.index("experiments")
        if len(parts) > index + 1:
            experiment = task.joinpath(*parts[: index + 2])
            return (
                (experiment / "published").is_file()
                and (experiment / "result.public.json").is_file()
            )
    return False


def _ensure_no_live_process(
    task: Path, process_records: Iterable[str] | None = None
) -> tuple[str, ...]:
    records = (
        tuple(task / relative for relative in process_records)
        if process_records is not None
        else tuple(
            path for path in task.rglob("process.json") if ".gc" not in path.parts
        )
    )
    relative_records: list[str] = []
    for process_record in records:
        if not process_record.is_file() or process_record.is_symlink():
            raise StateError("managed process record changed during garbage collection")
        relative = _relative(task, process_record)
        relative_records.append(relative)
        if recorded_process_is_live(process_record.parent):
            raise StateError(
                f"managed process is still live: {_relative(task, process_record.parent)}"
            )
    return tuple(sorted(relative_records))


def _managed_homes(
    task: Path, *, scope: Path | None = None
) -> tuple[tuple[Path, Path, bool], ...]:
    homes: list[tuple[Path, Path, bool]] = []
    search_root = task if scope is None else scope
    for started in search_root.rglob("started.json"):
        if started.parent.name == ".gc" or started.is_symlink():
            continue
        try:
            value = json.loads(started.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        environment = value.get("environment") if isinstance(value, Mapping) else None
        if not isinstance(environment, Mapping):
            continue
        process_directory = started.parent
        terminal = _process_terminal(process_directory, task)
        for name in ("HOME", "TMPDIR", "CODEX_HOME", "PYTHONPYCACHEPREFIX"):
            raw = environment.get(name)
            if not isinstance(raw, str) or not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                continue
            resolved = path.resolve(strict=False)
            if _inside(task, resolved):
                homes.append((resolved, process_directory, terminal))
    return tuple(dict.fromkeys(homes))


def _action(
    *,
    rule: str,
    kind: str,
    inputs: Iterable[str],
    outputs: Iterable[str] = (),
    depends_on: Iterable[str] = (),
    expected_bytes: int = 0,
    preconditions: Mapping[str, Any],
    status: str = "planned",
    reason: str | None = None,
) -> dict[str, Any]:
    core = {
        "rule_id": rule,
        "type": kind,
        "inputs": sorted(inputs),
        "outputs": sorted(outputs),
        "depends_on": sorted(depends_on),
        "expected_bytes": expected_bytes,
        "precondition_hash": _sha256(_json_bytes(preconditions)),
        "initial_status": status,
        "reason": reason,
    }
    return {"action_id": _sha256(_json_bytes(core))[:24], **core, "preconditions": dict(preconditions)}


def _sanitize_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise StateError("dependency source contains credentials")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _dependency_sources(lock: Path, runtime: Path) -> list[dict[str, str]]:
    value = tomllib.loads(lock.read_text(encoding="utf-8"))
    sources: set[tuple[str, str]] = set()
    for package in value.get("package", []):
        if not isinstance(package, Mapping) or not isinstance(package.get("source"), Mapping):
            continue
        source = package["source"]
        for kind in ("registry", "git"):
            raw = source.get(kind)
            if isinstance(raw, str):
                sources.add((kind, _sanitize_url(raw)))
        for kind in ("directory", "editable", "path"):
            raw = source.get(kind)
            if not isinstance(raw, str):
                continue
            candidate = (runtime / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
            if not candidate.exists():
                raise StateError("local dependency source cannot be validated")
            completed = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "HEAD"],
                check=False, capture_output=True, text=True,
            )
            if completed.returncode:
                raise StateError("local dependency has no durable Git identity")
            sources.add((kind, completed.stdout.strip()))
    return [{"kind": kind, "identity": identity} for kind, identity in sorted(sources)]


def _provenance_material(
    task: Path,
) -> tuple[dict[str, Any], dict[str, bytes], Path, Path] | None:
    if not (task / "approval.json").is_file():
        return None
    try:
        readiness = json.loads((task / "setup" / "readiness.public.json").read_text())
        setup = json.loads((task / "setup.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("approved setup provenance is invalid") from error
    staging = readiness.get("staging")
    if not isinstance(staging, Mapping) or not all(
        isinstance(staging.get(name), str) for name in ("subject", "environment", "evaluator", "runtime")
    ):
        return None
    roots = {name: Path(staging[name]).resolve() for name in staging}
    staging_root = roots["runtime"].parent
    if not _inside(task, staging_root) or any(path.parent != staging_root for path in roots.values()):
        raise StateError("setup staging paths are not one owned root")
    lock = roots["runtime"] / "uv.lock"
    project = roots["runtime"] / "pyproject.toml"
    if not lock.is_file() or not project.is_file():
        raise StateError("setup dependency inputs are missing")
    if _sha256(lock.read_bytes()) != readiness.get("dependency_lock_sha256"):
        raise StateError("setup dependency lock changed")
    owned = readiness.get("owned_files")
    if _sha256(_json_bytes(owned)) != readiness.get("owned_files_sha256"):
        raise StateError("setup owned-file record changed")
    from .setup import _tree_hash
    for name in ("subject", "environment", "evaluator"):
        actual = _tree_hash(roots[name], exclude_private=name == "evaluator")
        if actual != readiness.get("tree_hashes", {}).get(name):
            raise StateError(f"setup {name} tree changed")
    for field in ("subject_commit", "environment_commit", "evaluator_commit"):
        commit = setup.get(field)
        repo = setup.get(field.removesuffix("_commit"))
        if not isinstance(commit, str) or not isinstance(repo, str):
            raise StateError("setup commit provenance is incomplete")
        completed = subprocess.run(
            ["git", "-C", repo, "cat-file", "-e", f"{commit}^{{commit}}"],
            check=False, capture_output=True,
        )
        if completed.returncode:
            raise StateError("approved setup commit is missing")
    pyvenv = Path(setup.get("workspace", "")) / ".venv" / "pyvenv.cfg"
    runtime_details: dict[str, str] = {}
    if pyvenv.is_file():
        for line in pyvenv.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, raw = line.split("=", 1)
                if key.strip() in {"implementation", "version_info", "uv"}:
                    runtime_details[key.strip()] = raw.strip()
    files = {"pyproject.toml": project.read_bytes(), "uv.lock": lock.read_bytes()}
    manifest = {
        "schema_version": 1,
        "reconstruction_strength": "dependency-lock",
        "claim": (
            "Retained inputs support dependency-resolution reconstruction and provenance "
            "auditing; they do not guarantee bit-for-bit environment reproduction."
        ),
        "files": [
            {"path": name, "bytes": len(content), "sha256": _sha256(content)}
            for name, content in sorted(files.items())
        ],
        "python": {
            "implementation": runtime_details.get("implementation", platform.python_implementation()),
            "version": runtime_details.get("version_info", platform.python_version()),
            "abi": sysconfig.get_config_var("SOABI"),
            "platform": platform.platform(),
        },
        "resolver": {"name": "uv", "version": runtime_details.get("uv"), "options": []},
        "sources": _dependency_sources(lock, roots["runtime"]),
    }
    return manifest, files, task / "setup" / "staging", roots["runtime"]


def _cache_actions(
    task: Path, canonical: set[str], *, scope: Path | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for home, process_directory, terminal in _managed_homes(task, scope=scope):
        candidates = [home / "pycache", *(path for path in home.rglob("__pycache__") if path.is_dir())]
        for candidate in candidates:
            if candidate in seen or not candidate.exists():
                continue
            seen.add(candidate)
            relative = _relative(task, candidate)
            if relative in canonical:
                skipped.append({"scope": relative, "reason": "canonical path", "count": 1})
                continue
            try:
                total, count, tree_hash = _tree_inventory(candidate)
                identity = _lstat_identity(candidate)
            except (OSError, StateError) as error:
                skipped.append({"scope": relative, "reason": str(error), "count": 1})
                continue
            status = "planned" if terminal else "blocked"
            reason = None if terminal else "recovery-critical stage is incomplete"
            action = _action(
                rule="managed-python-cache", kind="quarantine_disposable_root",
                inputs=(relative,), expected_bytes=total,
                preconditions={"identity": identity, "tree_hash": tree_hash, "path_count": count,
                               "process": _relative(task, process_directory),
                               "task_identity": _stable_identity(task),
                               "parent_identity": _stable_identity(candidate.parent)},
                status=status, reason=reason,
            )
            actions.append(action)
            if status == "planned":
                actions.append(_action(
                    rule="managed-python-cache", kind="remove_quarantined_root",
                    inputs=(f"@quarantine/{action['action_id']}",),
                    depends_on=(action["action_id"],), preconditions={"source_action": action["action_id"]},
                    status="conditional",
                ))
    return actions, skipped


def _scratch_actions(
    task: Path, canonical: set[str], *, scope: Path | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for home, process_directory, terminal in _managed_homes(task, scope=scope):
        synthetic = [path for path in home.rglob("codex-arg*") if path.is_dir()]
        roots = [path for path in synthetic if _synthetic_scratch_root(path)]
        locks = [
            path
            for pattern in (".lock", "lock")
            for path in home.rglob(pattern)
            if path.is_file()
            and not path.is_symlink()
            and path.stat().st_size == 0
            and not any(_inside(root, path) for root in roots)
        ]
        for candidate in (*roots, *locks):
            if candidate in seen or not candidate.exists():
                continue
            seen.add(candidate)
            relative = _relative(task, candidate)
            if relative in canonical:
                skipped.append({"scope": relative, "reason": "canonical path", "count": 1})
                continue
            allow_symlinks = candidate in roots
            try:
                total, count, tree_hash = _tree_inventory(
                    candidate, allow_symlinks=allow_symlinks
                )
                identity = _lstat_identity(candidate)
            except (OSError, StateError) as error:
                skipped.append({"scope": relative, "reason": str(error), "count": 1})
                continue
            rule = "synthetic-scratch-target" if candidate in roots else "scratch-zero-lock"
            status = "planned" if terminal else "blocked"
            quarantine = _action(
                rule=rule,
                kind="quarantine_disposable_root",
                inputs=(relative,),
                expected_bytes=total,
                preconditions={
                    "identity": identity,
                    "tree_hash": tree_hash,
                    "path_count": count,
                    "process": _relative(task, process_directory),
                    "allow_symlinks": allow_symlinks,
                    "task_identity": _stable_identity(task),
                    "parent_identity": _stable_identity(candidate.parent),
                },
                status=status,
                reason=None if terminal else "recovery-critical stage is incomplete",
            )
            actions.append(quarantine)
            if status == "planned":
                actions.append(
                    _action(
                        rule=rule,
                        kind="remove_quarantined_root",
                        inputs=(f"@quarantine/{quarantine['action_id']}",),
                        depends_on=(quarantine["action_id"],),
                        preconditions={"source_action": quarantine["action_id"]},
                        status="conditional",
                    )
                )
    return actions, skipped


def _setup_actions(task: Path, canonical: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        material = _provenance_material(task)
    except StateError as error:
        staging = task / "setup" / "staging"
        if staging.exists():
            actions.append(_action(
                rule="completed-setup-staging", kind="quarantine_disposable_root",
                inputs=(_relative(task, staging),), preconditions={"error": str(error)},
                status="blocked", reason=str(error),
            ))
        return actions, skipped
    if material is None:
        return actions, skipped
    manifest, files, staging, runtime = material
    destination = task / "setup" / "dependency-provenance"
    expected = {**files, "manifest.public.json": _json_bytes(manifest) + b"\n"}
    already = destination.is_dir() and all(
        (destination / name).is_file() and (destination / name).read_bytes() == content
        for name, content in expected.items()
    )
    promotion_id: str | None = None
    if not already:
        promotion = _action(
            rule="runtime-dependency-provenance", kind="promote_dependency_provenance",
            inputs=(_relative(task, runtime / "pyproject.toml"),
                    _relative(task, runtime / "uv.lock")),
            outputs=tuple(_relative(task, destination / name) for name in expected),
            expected_bytes=sum(len(value) for value in expected.values()),
            preconditions={"manifest": manifest, "file_hashes": {name: _sha256(value) for name, value in files.items()}},
        )
        actions.append(promotion)
        promotion_id = promotion["action_id"]
    try:
        total, count, tree_hash = _tree_inventory(staging)
    except (OSError, StateError) as error:
        actions.append(
            _action(
                rule="completed-setup-staging",
                kind="quarantine_disposable_root",
                inputs=(_relative(task, staging),),
                depends_on=((promotion_id,) if promotion_id else ()),
                preconditions={"error": str(error)},
                status="blocked",
                reason=str(error),
            )
        )
        return actions, skipped
    deletion = _action(
        rule="completed-setup-staging", kind="quarantine_disposable_root",
        inputs=(_relative(task, staging),), depends_on=((promotion_id,) if promotion_id else ()),
        expected_bytes=total,
        preconditions={
            "identity": _lstat_identity(staging),
            "tree_hash": tree_hash,
            "path_count": count,
            "task_identity": _stable_identity(task),
            "parent_identity": _stable_identity(staging.parent),
        },
        status="conditional" if promotion_id else "planned",
    )
    actions.append(deletion)
    actions.append(_action(
        rule="completed-setup-staging", kind="remove_quarantined_root",
        inputs=(f"@quarantine/{deletion['action_id']}",), depends_on=(deletion["action_id"],),
        preconditions={"source_action": deletion["action_id"]}, status="conditional",
    ))
    return actions, skipped


def _abandoned_worktree_actions(
    task: Path, canonical: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    worktrees = task / "worktrees"
    if not worktrees.is_dir() or not (task / "task.yaml").is_file():
        return actions, skipped
    config = load_task(task / "task.yaml")
    listed = subprocess.run(
        ["git", "-C", str(config.repo), "worktree", "list", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode:
        return actions, [{"scope": "worktrees", "reason": "Git worktree registry unavailable", "count": 1}]
    registered = {
        Path(line.removeprefix("worktree ")).resolve()
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    }
    for candidate in sorted(worktrees.glob("[0-9][0-9][0-9][0-9][0-9][0-9]-candidate")):
        if candidate.resolve() in registered:
            continue
        identifier = candidate.name.removesuffix("-candidate")
        result_path = task / "experiments" / identifier / "result.public.json"
        published = result_path.parent / "published"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            commit = result["candidate"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
        durable = subprocess.run(
            ["git", "-C", str(config.repo), "cat-file", "-e", f"{commit}^{{commit}}"],
            check=False,
            capture_output=True,
        )
        if not published.is_file() or durable.returncode:
            continue
        relative = _relative(task, candidate)
        if relative in canonical:
            continue
        try:
            total, count, tree_hash = _tree_inventory(candidate)
        except (OSError, StateError) as error:
            skipped.append({"scope": relative, "reason": str(error), "count": 1})
            continue
        quarantine = _action(
            rule="abandoned-durable-candidate-worktree",
            kind="quarantine_disposable_root",
            inputs=(relative,),
            expected_bytes=total,
            preconditions={
                "identity": _lstat_identity(candidate),
                "tree_hash": tree_hash,
                "path_count": count,
                "candidate_commit": commit,
                "experiment": identifier,
                "task_identity": _stable_identity(task),
                "parent_identity": _stable_identity(candidate.parent),
            },
        )
        actions.extend(
            (
                quarantine,
                _action(
                    rule="abandoned-durable-candidate-worktree",
                    kind="remove_quarantined_root",
                    inputs=(f"@quarantine/{quarantine['action_id']}",),
                    depends_on=(quarantine["action_id"],),
                    preconditions={"source_action": quarantine["action_id"]},
                    status="conditional",
                ),
            )
        )
    return actions, skipped


def _durable_experiment_entry(task: Path, identifier: str) -> bool:
    source = f"experiment:{identifier}"
    entries = task / "exploration" / "entries"
    for path in entries.glob("*.public.json") if entries.is_dir() else ():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping) and value.get("source") == source:
            return True
    return False


def _experiment_cache_actions(
    task: Path, experiment: Path, canonical: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    record_path = experiment / "experiment.json"
    result = experiment / "result.public.json"
    published = experiment / "published"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("experiment cleanup lifecycle is invalid") from error
    if (
        not isinstance(record, Mapping)
        or record.get("state") != "COMPLETE"
        or not result.is_file()
        or not published.is_file()
        or not _durable_experiment_entry(task, experiment.name)
    ):
        raise StateError("experiment is not durably published")
    manifest_path = experiment / "runtime" / "bytecode-cache.private.json"
    if not manifest_path.exists():
        return [], []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("experiment bytecode cache manifest is invalid") from error
    expected_fields = {
        "schema_version",
        "experiment_id",
        "request_sha256",
        "approval_sha256",
        "runtime_manifest_sha256",
        "python",
        "cache_path",
    }
    if (
        not isinstance(manifest, Mapping)
        or set(manifest) != expected_fields
        or manifest.get("schema_version") != 1
        or manifest.get("experiment_id") != int(experiment.name)
    ):
        raise StateError("experiment bytecode cache manifest is invalid")
    request = experiment / "request.public.json"
    if _sha256(request.read_bytes()) != manifest.get("request_sha256"):
        raise StateError("experiment bytecode cache request identity changed")
    if (
        _sha256((task / "approval.json").read_bytes())
        != manifest.get("approval_sha256")
        or _sha256((task / "evaluator.manifest.json").read_bytes())
        != manifest.get("runtime_manifest_sha256")
    ):
        raise StateError("experiment bytecode runtime identity changed")
    raw_cache = manifest.get("cache_path")
    if not isinstance(raw_cache, str):
        raise StateError("experiment bytecode cache path is invalid")
    cache = task / raw_cache
    expected_parent = experiment / "runtime" / "python-bytecode"
    python = manifest.get("python")
    if (
        not isinstance(python, Mapping)
        or set(python) != {"cache_tag", "implementation", "version", "executable"}
        or not all(isinstance(value, str) and value for value in python.values())
        or cache.name != python["cache_tag"]
        or cache.parent != expected_parent
        or (experiment / "runtime").is_symlink()
        or expected_parent.is_symlink()
        or cache.is_symlink()
        or not _inside(task, cache.resolve())
    ):
        raise StateError("experiment bytecode cache path is invalid")
    if not cache.exists():
        return [], []
    if not cache.is_dir():
        raise StateError("experiment bytecode cache path is invalid")
    relative = _relative(task, cache)
    if relative in canonical:
        return [], [{"scope": relative, "reason": "canonical path", "count": 1}]
    total, count, tree_hash = _tree_inventory(cache)
    quarantine = _action(
        rule="experiment-version-bytecode",
        kind="quarantine_disposable_root",
        inputs=(relative,),
        expected_bytes=total,
        preconditions={
            "identity": _lstat_identity(cache),
            "tree_hash": tree_hash,
            "path_count": count,
            "task_identity": _stable_identity(task),
            "parent_identity": _stable_identity(cache.parent),
            "experiment": experiment.name,
            "manifest_sha256": _sha256(manifest_path.read_bytes()),
        },
    )
    remove = _action(
        rule="experiment-version-bytecode",
        kind="remove_quarantined_root",
        inputs=(f"@quarantine/{quarantine['action_id']}",),
        depends_on=(quarantine["action_id"],),
        preconditions={"source_action": quarantine["action_id"]},
        status="conditional",
    )
    return [quarantine, remove], []


def build_gc_plan(task_directory: Path) -> dict[str, Any]:
    task = task_directory.resolve()
    if not task.is_dir() or task.is_symlink():
        raise StateError("task directory is invalid")
    process_records = _ensure_no_live_process(task)
    initial_snapshot = canonical_snapshot(task)
    canonical = {entry["path"] for entry in initial_snapshot["entries"]}
    setup_actions, setup_skipped = _setup_actions(task, canonical)
    cache_actions, skipped = _cache_actions(task, canonical)
    scratch_actions, scratch_skipped = _scratch_actions(task, canonical)
    worktree_actions, worktree_skipped = _abandoned_worktree_actions(task, canonical)
    version_actions: list[dict[str, Any]] = []
    version_skipped: list[dict[str, Any]] = []
    experiments = task / "experiments"
    for experiment in sorted(experiments.glob("[0-9]" * 6)) if experiments.is_dir() else ():
        if not (experiment / "runtime" / "bytecode-cache.private.json").is_file():
            continue
        try:
            actions, reasons = _experiment_cache_actions(task, experiment, canonical)
        except (OSError, StateError) as error:
            version_skipped.append(
                {
                    "scope": _relative(task, experiment),
                    "reason": str(error),
                    "count": 1,
                }
            )
            continue
        version_actions.extend(actions)
        version_skipped.extend(reasons)
    disposable_actions = [
        *version_actions,
        *cache_actions,
        *scratch_actions,
        *worktree_actions,
    ]
    setup_deletions = [
        task / item["inputs"][0]
        for item in setup_actions
        if item["type"] == "quarantine_disposable_root"
        and item["initial_status"] != "blocked"
    ]
    if setup_deletions:
        disposable_actions = [
            item for item in disposable_actions
            if item["type"] == "remove_quarantined_root"
            or not any(_inside(root, task / item["inputs"][0]) for root in setup_deletions)
        ]
        retained_ids = {item["action_id"] for item in disposable_actions}
        disposable_actions = [
            item for item in disposable_actions
            if all(dependency in retained_ids for dependency in item["depends_on"])
        ]
    rank = {
        "promote_dependency_provenance": 0,
        "quarantine_disposable_root": 1,
        "remove_quarantined_root": 2,
    }
    actions = sorted(
        (*disposable_actions, *setup_actions),
        key=lambda item: (rank[item["type"]], item["action_id"]),
    )
    future_outputs = {
        output
        for action in actions
        if action["type"] == "promote_dependency_provenance"
        for output in action["outputs"]
    }
    future_roots = sorted({str(Path(output).parent) for output in future_outputs})
    snapshot = canonical_snapshot(task, exclude=future_roots)
    core = {
        "schema_version": _GC_SCHEMA_VERSION,
        "task_id": task.name,
        "canonical_snapshot": snapshot,
        "future_canonical_outputs": sorted(future_outputs),
        "future_canonical_roots": future_roots,
        "process_records": process_records,
        "actions": actions,
        "skipped": sorted(
            (
                *skipped,
                *scratch_skipped,
                *worktree_skipped,
                *version_skipped,
                *setup_skipped,
            ),
            key=lambda item: (item["scope"], item["reason"]),
        ),
    }
    return {**core, "plan_hash": _sha256(_json_bytes(core))}


def build_experiment_gc_plan(
    task_directory: Path, experiment_directory: Path
) -> dict[str, Any]:
    task = task_directory.resolve()
    experiment = experiment_directory.resolve()
    if experiment.parent != task / "experiments" or not experiment.name.isdigit():
        raise StateError("experiment cleanup scope is invalid")
    process_records = _ensure_no_live_process(
        task,
        tuple(
            _relative(task, path)
            for path in experiment.rglob("process.json")
            if ".gc" not in path.parts
        ),
    )
    snapshot = experiment_canonical_snapshot(task, experiment)
    canonical = {entry["path"] for entry in snapshot["entries"]}
    version_actions, version_skipped = _experiment_cache_actions(
        task, experiment, canonical
    )
    cache_actions, cache_skipped = _cache_actions(
        task, canonical, scope=experiment
    )
    scratch_actions, scratch_skipped = _scratch_actions(
        task, canonical, scope=experiment
    )
    rank = {"quarantine_disposable_root": 0, "remove_quarantined_root": 1}
    actions = sorted(
        (*version_actions, *cache_actions, *scratch_actions),
        key=lambda item: (rank[item["type"]], item["action_id"]),
    )
    core = {
        "schema_version": _GC_SCHEMA_VERSION,
        "task_id": task.name,
        "scope": {"kind": "experiment", "id": experiment.name},
        "canonical_snapshot": snapshot,
        "future_canonical_outputs": [],
        "future_canonical_roots": [],
        "process_records": process_records,
        "actions": actions,
        "skipped": sorted(
            (*version_skipped, *cache_skipped, *scratch_skipped),
            key=lambda item: (item["scope"], item["reason"]),
        ),
    }
    return {**core, "plan_hash": _sha256(_json_bytes(core))}


def _revalidate_source(task: Path, action: Mapping[str, Any]) -> Path:
    relative = action["inputs"][0]
    source = task / relative
    if source.is_symlink() or not source.exists():
        raise StateError("disposable source identity changed")
    expected = action["preconditions"]
    if (
        _stable_identity(task) != expected["task_identity"]
        or _stable_identity(source.parent) != expected["parent_identity"]
    ):
        raise StateError("task or disposable parent identity changed")
    total, count, tree_hash = _tree_inventory(
        source, allow_symlinks=bool(expected.get("allow_symlinks"))
    )
    if (
        _lstat_identity(source) != expected["identity"]
        or tree_hash != expected["tree_hash"]
        or count != expected["path_count"]
        or total != action["expected_bytes"]
    ):
        raise StateError("disposable source preconditions changed")
    return source


def _promote(task: Path, action: Mapping[str, Any]) -> None:
    output_root = task / "setup" / "dependency-provenance"
    if output_root.is_symlink():
        raise StateError("dependency provenance target is a symlink")
    output_root.mkdir(parents=True, exist_ok=True)
    outputs = {Path(item).name: item for item in action["outputs"]}
    for source_relative in action["inputs"]:
        source = task / source_relative
        expected = action["preconditions"]["file_hashes"][source.name]
        content = source.read_bytes()
        if _sha256(content) != expected:
            raise StateError("dependency provenance source changed")
        target = task / outputs[source.name]
        if target.exists() and target.read_bytes() != content:
            raise StateError("dependency provenance target changed")
        if not target.exists():
            atomic_write_bytes(target, content)
    manifest_target = task / outputs["manifest.public.json"]
    manifest_content = _json_bytes(action["preconditions"]["manifest"]) + b"\n"
    if manifest_target.exists() and manifest_target.read_bytes() != manifest_content:
        raise StateError("dependency provenance manifest changed")
    if not manifest_target.exists():
        atomic_write_bytes(manifest_target, manifest_content)


def _rename_to_quarantine(task: Path, source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.lstat().st_dev != destination.parent.lstat().st_dev:
        raise StateError("quarantine must be on the source filesystem")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_parent_fd = os.open(source.parent, flags)
    destination_parent_fd = os.open(destination.parent, flags)
    try:
        os.rename(source.name, destination.name, src_dir_fd=source_parent_fd, dst_dir_fd=destination_parent_fd)
    finally:
        os.close(source_parent_fd)
        os.close(destination_parent_fd)


def _save_journal(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_json(path, value)


def _plan_snapshot(task: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    scope = plan.get("scope")
    if isinstance(scope, Mapping) and scope.get("kind") == "experiment":
        identifier = scope.get("id")
        if not isinstance(identifier, str) or not identifier.isdigit():
            raise StateError("garbage-collection scope is invalid")
        return experiment_canonical_snapshot(
            task, task / "experiments" / identifier
        )
    return canonical_snapshot(task, exclude=plan.get("future_canonical_roots", ()))


def _execute_plan(task: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    gc_root = task / ".gc"
    journal_path = gc_root / "transaction.json"
    quarantine = gc_root / "quarantine" / plan["plan_hash"]
    gc_root.mkdir(parents=True, exist_ok=True)
    if journal_path.is_file():
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("garbage-collection journal is invalid") from error
        if journal.get("plan") != plan or not isinstance(journal.get("execution"), Mapping):
            raise StateError("garbage-collection journal changed")
        execution = dict(journal["execution"])
        journal["execution"] = execution
    else:
        execution = {action["action_id"]: action["initial_status"] for action in plan["actions"]}
        journal = {"schema_version": 1, "plan": plan, "execution": execution}
        _save_journal(journal_path, journal)
    action_by_id = {action["action_id"]: action for action in plan["actions"]}
    failed = False
    for action in plan["actions"]:
        identifier = action["action_id"]
        if execution[identifier] in {"removed", "skipped", "quarantined"}:
            continue
        if execution[identifier] in {"failed", "remaining"}:
            execution[identifier] = action["initial_status"]
            journal.get("errors", {}).pop(identifier, None)
        if execution[identifier] == "blocked":
            execution[identifier] = "skipped"
            _save_journal(journal_path, journal)
            continue
        dependencies = [execution[item] for item in action["depends_on"]]
        if any(value in {"failed", "skipped", "remaining"} for value in dependencies):
            execution[identifier] = "remaining"
            failed = True
            _save_journal(journal_path, journal)
            continue
        if action["type"] == "remove_quarantined_root" and any(value != "quarantined" for value in dependencies):
            execution[identifier] = "remaining"
            failed = True
            _save_journal(journal_path, journal)
            continue
        try:
            _ensure_no_live_process(task, plan.get("process_records"))
            if _plan_snapshot(task, plan) != plan["canonical_snapshot"]:
                raise StateError("canonical task state changed during garbage collection")
            if action["type"] == "promote_dependency_provenance":
                _promote(task, action)
                execution[identifier] = "removed"
            elif action["type"] == "quarantine_disposable_root":
                target = quarantine / identifier
                source = task / action["inputs"][0]
                if not source.exists() and target.exists() and not target.is_symlink():
                    total, count, tree_hash = _tree_inventory(
                        target,
                        allow_symlinks=bool(
                            action["preconditions"].get("allow_symlinks")
                        ),
                    )
                    expected = action["preconditions"]
                    if (
                        total != action["expected_bytes"]
                        or count != expected["path_count"]
                        or tree_hash != expected["tree_hash"]
                    ):
                        raise StateError("quarantined disposable root identity changed")
                elif source.exists() and not target.exists():
                    source = _revalidate_source(task, action)
                    _rename_to_quarantine(task, source, target)
                else:
                    raise StateError("disposable source and quarantine state are ambiguous")
                execution[identifier] = "quarantined"
            elif action["type"] == "remove_quarantined_root":
                source_action = action_by_id[action["depends_on"][0]]
                target = quarantine / source_action["action_id"]
                if target.is_symlink():
                    raise StateError("quarantined disposable root is invalid")
                if not target.exists():
                    # The quarantined identity was already durably recorded. A
                    # missing target therefore means deletion completed before
                    # its final journal update.
                    execution[identifier] = "removed"
                elif target.is_dir():
                    shutil.rmtree(target)
                    execution[identifier] = "removed"
                else:
                    target.unlink()
                    execution[identifier] = "removed"
            else:
                raise StateError("unknown garbage-collection action")
        except (OSError, StateError) as error:
            execution[identifier] = "failed"
            journal.setdefault("errors", {})[identifier] = str(error)
            failed = True
        _save_journal(journal_path, journal)
    if _plan_snapshot(task, plan) != plan["canonical_snapshot"]:
        failed = True
        journal.setdefault("errors", {})["canonical"] = "canonical task state changed"
        _save_journal(journal_path, journal)
    removed_ids = {
        action["depends_on"][0]
        for action in plan["actions"]
        if action["type"] == "remove_quarantined_root" and execution[action["action_id"]] == "removed"
    }
    summary = {
        "schema_version": 1,
        "plan_hash": plan["plan_hash"],
        "scope": plan.get("scope", {"kind": "task"}),
        "reclaimed_bytes": sum(
            action["expected_bytes"] for action in plan["actions"] if action["action_id"] in removed_ids
        ),
        "removed_path_count": len(removed_ids),
        "execution": execution,
        "failed": failed,
    }
    atomic_write_json(task / "gc.private.json", summary)
    if not failed:
        journal_path.unlink(missing_ok=True)
        shutil.rmtree(quarantine, ignore_errors=True)
        try:
            (gc_root / "quarantine").rmdir()
            gc_root.rmdir()
        except OSError:
            pass
    return summary


def run_gc(task_directory: Path, *, dry_run: bool) -> dict[str, Any]:
    task = task_directory.resolve()
    journal_path = task / ".gc" / "transaction.json"
    if journal_path.is_file():
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("garbage-collection journal is invalid") from error
        plan = journal.get("plan")
        if not isinstance(plan, Mapping):
            raise StateError("garbage-collection journal is invalid")
        if dry_run:
            return _present(plan, dry_run=True, execution=journal.get("execution"))
        summary = _execute_plan(task, plan)
        recovered = _present(
            plan,
            dry_run=False,
            execution=summary["execution"],
            summary=summary,
        )
        if summary["failed"]:
            return recovered
    plan = build_gc_plan(task)
    if dry_run:
        return _present(plan, dry_run=True)
    summary = _execute_plan(task, plan)
    return _present(plan, dry_run=False, execution=summary["execution"], summary=summary)


def run_experiment_gc(
    task_directory: Path, experiment_directory: Path
) -> dict[str, Any]:
    """Recover prior cleanup, then clean one durably completed experiment."""
    task = task_directory.resolve()
    journal_path = task / ".gc" / "transaction.json"
    if journal_path.is_file():
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError("garbage-collection journal is invalid") from error
        prior = journal.get("plan")
        if not isinstance(prior, Mapping):
            raise StateError("garbage-collection journal is invalid")
        recovery = _execute_plan(task, prior)
        if recovery["failed"]:
            return _present(
                prior,
                dry_run=False,
                execution=recovery["execution"],
                summary=recovery,
            )
    plan = build_experiment_gc_plan(task, experiment_directory)
    summary = _execute_plan(task, plan)
    return _present(
        plan,
        dry_run=False,
        execution=summary["execution"],
        summary=summary,
    )


def recover_gc_transaction(task_directory: Path) -> dict[str, Any] | None:
    task = task_directory.resolve()
    journal_path = task / ".gc" / "transaction.json"
    if not journal_path.is_file():
        return None
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError("garbage-collection journal is invalid") from error
    plan = journal.get("plan")
    if not isinstance(plan, Mapping):
        raise StateError("garbage-collection journal is invalid")
    summary = _execute_plan(task, plan)
    return _present(
        plan,
        dry_run=False,
        execution=summary["execution"],
        summary=summary,
    )


def _present(
    plan: Mapping[str, Any], *, dry_run: bool,
    execution: Mapping[str, str] | None = None,
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    deletion_actions = [
        item for item in plan["actions"] if item["type"] == "quarantine_disposable_root"
    ]
    blocked: dict[str, int] = {}
    for action in deletion_actions:
        if action["initial_status"] == "blocked":
            reason = action.get("reason") or "blocked by retention policy"
            blocked[reason] = blocked.get(reason, 0) + 1
    skipped_reasons: dict[str, int] = {}
    for item in plan["skipped"]:
        reason = item["reason"]
        skipped_reasons[reason] = skipped_reasons.get(reason, 0) + int(
            item.get("count", 1)
        )
    planned_bytes = sum(
        item["expected_bytes"]
        for item in deletion_actions
        if item["initial_status"] != "blocked"
    )
    planned_paths = sum(
        item["preconditions"].get("path_count", 0)
        for item in deletion_actions
        if item["initial_status"] != "blocked"
    )
    return {
        "schema_version": 1,
        "task_id": plan["task_id"],
        "scope": plan.get("scope", {"kind": "task"}),
        "dry_run": dry_run,
        "mutation_occurred": bool(summary and summary.get("removed_path_count")),
        "plan_hash": plan["plan_hash"],
        "eligible_bytes": planned_bytes,
        "eligible_path_count": planned_paths,
        "reclaimed_bytes": summary.get("reclaimed_bytes", 0) if summary else 0,
        "removed_path_count": summary.get("removed_path_count", 0) if summary else 0,
        "planned_totals": {"bytes": planned_bytes, "paths": planned_paths},
        "actual_totals": {
            "bytes": summary.get("reclaimed_bytes", 0) if summary else 0,
            "roots": summary.get("removed_path_count", 0) if summary else 0,
        },
        "actions": [{key: value for key, value in item.items() if key != "preconditions"} for item in plan["actions"]],
        "execution": dict(execution) if execution is not None else None,
        "skipped": plan["skipped"],
        "reasons": {
            "blocked": [
                {"reason": reason, "count": count}
                for reason, count in sorted(blocked.items())
            ],
            "skipped": [
                {"reason": reason, "count": count}
                for reason, count in sorted(skipped_reasons.items())
            ],
        },
        "failed": bool(summary and summary.get("failed")),
    }
