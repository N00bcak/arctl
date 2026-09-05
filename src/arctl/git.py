"""Git operations whose invariants are owned by the controller."""

from __future__ import annotations

import json
import subprocess
from fnmatch import fnmatchcase
from importlib.util import source_from_cache
from pathlib import Path
from stat import S_ISREG
from typing import Sequence, cast

from .errors import ResearchMiss, StateError
from .storage import atomic_write_json


def _git(repo: Path, arguments: Sequence[str], *, check: bool = True) -> str:
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


def _git_paths(repo: Path, arguments: Sequence[str]) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise StateError(f"git {' '.join(arguments)} failed: {detail}")
    return tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    )


def _untracked_paths(repo: Path) -> tuple[str, ...]:
    visible = _git_paths(
        repo, ["ls-files", "--others", "--exclude-standard", "-z"]
    )
    ignored = _git_paths(
        repo,
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
    )
    return tuple(sorted(set((*visible, *ignored))))


def _is_regular_file(path: Path) -> bool:
    try:
        return S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _is_python_cache_artifact(repo: Path, path: str) -> bool:
    relative = Path(path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parent.name != "__pycache__"
        or relative.suffix != ".pyc"
    ):
        return False
    target = repo / relative
    if not _is_regular_file(target):
        return False
    try:
        source = Path(source_from_cache(str(target)))
    except ValueError:
        return False
    return source.parent == target.parent.parent and _is_regular_file(source)


def _load_runtime_artifact_events(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"runtime artifact audit is invalid: {path}") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"events"}
        or not isinstance(value["events"], list)
    ):
        raise StateError(f"runtime artifact audit is invalid: {path}")
    events: list[dict[str, object]] = []
    stages: set[str] = set()
    for event in value["events"]:
        if (
            not isinstance(event, dict)
            or set(event) != {"stage", "discarded_paths"}
            or not isinstance(event["stage"], str)
            or not event["stage"]
            or event["stage"] in stages
            or not isinstance(event["discarded_paths"], list)
            or any(not isinstance(item, str) or not item for item in event["discarded_paths"])
            or event["discarded_paths"] != sorted(set(event["discarded_paths"]))
        ):
            raise StateError(f"runtime artifact audit is invalid: {path}")
        stages.add(event["stage"])
        events.append(event)
    return events


def normalize_runtime_artifacts(
    repo: Path,
    *,
    stage: str,
    audit_path: Path | None = None,
) -> tuple[str, ...]:
    """Discard canonical untracked Python caches without relaxing candidate scope."""
    if not stage:
        raise ValueError("runtime artifact stage must not be empty")
    if audit_path is not None:
        resolved_repo = repo.resolve()
        resolved_audit = audit_path.resolve()
        if resolved_audit == resolved_repo or resolved_repo in resolved_audit.parents:
            raise ValueError("runtime artifact audit must be outside the candidate worktree")

    eligible = tuple(
        path
        for path in _untracked_paths(repo)
        if _is_python_cache_artifact(repo, path)
    )
    discarded = eligible
    if audit_path is not None:
        events = _load_runtime_artifact_events(audit_path)
        saved = next((event for event in events if event["stage"] == stage), None)
        if saved is not None:
            discarded = tuple(cast(list[str], saved["discarded_paths"]))
            unexpected = sorted(set(eligible) - set(discarded))
            if unexpected:
                raise StateError(
                    "runtime artifact set changed while recovering stage "
                    f"{stage}: {unexpected[0]}"
                )
        elif discarded:
            events.append({"stage": stage, "discarded_paths": list(discarded)})
            atomic_write_json(
                audit_path,
                {"events": events},
            )

    eligible_set = set(eligible)
    parents: set[Path] = set()
    for relative in discarded:
        target = repo / relative
        if not target.exists() and not target.is_symlink():
            continue
        if relative not in eligible_set:
            raise StateError(
                "recorded runtime artifact is no longer an untracked canonical "
                f"Python cache: {relative}"
            )
        target.unlink()
        parents.add(target.parent)
    for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    return discarded


def resolve_commit(repo: Path, revision: str) -> str:
    return _git(repo, ["rev-parse", "--verify", f"{revision}^{{commit}}"])


def ensure_clean_worktree(repo: Path) -> None:
    if _git(repo, ["status", "--porcelain", "--untracked-files=all"]):
        raise StateError(f"Git worktree is not clean: {repo}")


def candidate_changed_paths(repo: Path, champion: str, candidate: str) -> tuple[str, ...]:
    output = _git(repo, ["diff", "--name-only", "-z", champion, candidate])
    return tuple(path for path in output.split("\0") if path)


def candidate_diff(repo: Path, champion: str, candidate: str) -> str:
    return _git(repo, ["diff", "--no-ext-diff", "--binary", champion, candidate])


def validate_changed_paths(
    paths: Sequence[str],
    *,
    editable_paths: Sequence[str],
    denied_paths: Sequence[str],
) -> None:
    for path in paths:
        if any(fnmatchcase(path, pattern) for pattern in denied_paths):
            raise StateError(f"candidate changed denied path: {path}")
        if not any(fnmatchcase(path, pattern) for pattern in editable_paths):
            raise StateError(f"candidate changed path outside editable paths: {path}")


def validate_candidate(
    repo: Path,
    *,
    champion: str,
    candidate: str,
    editable_paths: Sequence[str],
    denied_paths: Sequence[str],
) -> tuple[str, ...]:
    champion_commit = resolve_commit(repo, champion)
    candidate_commit = resolve_commit(repo, candidate)
    parents = _git(repo, ["show", "-s", "--format=%P", candidate_commit]).split()
    if parents != [champion_commit]:
        raise StateError("candidate must have exactly the approved champion as its parent")
    if _git(repo, ["rev-parse", f"{champion_commit}^{{tree}}"]) == _git(
        repo, ["rev-parse", f"{candidate_commit}^{{tree}}"]
    ):
        raise StateError("candidate tree is unchanged")

    paths = candidate_changed_paths(repo, champion_commit, candidate_commit)
    validate_changed_paths(
        paths,
        editable_paths=editable_paths,
        denied_paths=denied_paths,
    )
    return paths


def create_detached_worktree(repo: Path, path: Path, revision: str) -> None:
    if path.exists():
        raise StateError(f"worktree path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(
        repo,
        ["worktree", "add", "--force", "--detach", str(path), resolve_commit(repo, revision)],
    )
    ensure_clean_worktree(path)


def remove_worktree(repo: Path, path: Path) -> None:
    if not path.exists():
        return
    _git(repo, ["worktree", "remove", "--force", str(path)])


def create_candidate_commit(
    worktree: Path,
    *,
    champion: str,
    editable_paths: Sequence[str],
    denied_paths: Sequence[str],
    prior_candidate_ref_prefix: str,
    message: str,
    runtime_artifact_audit: Path | None = None,
) -> tuple[str, tuple[str, ...]]:
    champion_commit = resolve_commit(worktree, champion)
    if resolve_commit(worktree, "HEAD") != champion_commit:
        raise StateError("research worktree no longer points at the starting champion")
    normalize_runtime_artifacts(
        worktree,
        stage="candidate-staging",
        audit_path=runtime_artifact_audit,
    )
    _git(worktree, ["add", "--all"])
    output = _git(worktree, ["diff", "--cached", "--name-only", "-z", champion_commit])
    paths = tuple(path for path in output.split("\0") if path)
    if not paths:
        raise ResearchMiss("unchanged", "candidate tree is unchanged")
    try:
        validate_changed_paths(
            paths,
            editable_paths=editable_paths,
            denied_paths=denied_paths,
        )
    except StateError as error:
        raise ResearchMiss("scope_violation", str(error)) from error
    tree = _git(worktree, ["write-tree"])
    if tree == _git(worktree, ["rev-parse", f"{champion_commit}^{{tree}}"]):
        raise ResearchMiss("unchanged", "candidate tree is unchanged")

    refs = _git(
        worktree,
        ["for-each-ref", "--format=%(objectname)", prior_candidate_ref_prefix],
    ).splitlines()
    for prior in refs:
        parents = _git(worktree, ["show", "-s", "--format=%P", prior]).split()
        if parents == [champion_commit] and _git(
            worktree, ["rev-parse", f"{prior}^{{tree}}"]
        ) == tree:
            raise ResearchMiss(
                "exact_duplicate",
                "the same candidate tree was already tested against this champion"
            )

    completed = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=arctl",
            "-c",
            "user.email=arctl@invalid",
            "commit-tree",
            tree,
            "-p",
            champion_commit,
            "-m",
            message,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise StateError(f"could not create candidate commit: {completed.stderr.strip()}")
    return completed.stdout.strip(), paths


def create_candidate_ref(repo: Path, ref: str, candidate: str) -> None:
    candidate_commit = resolve_commit(repo, candidate)
    existing = _git(repo, ["rev-parse", "--verify", "--quiet", ref], check=False)
    if existing:
        if existing != candidate_commit:
            raise StateError("candidate ref already points at a different commit")
        return
    completed = subprocess.run(
        ["git", "-C", str(repo), "update-ref", ref, candidate_commit, "0" * 40],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise StateError("could not create candidate ref")


def delete_ref(repo: Path, ref: str, expected: str) -> None:
    expected_commit = resolve_commit(repo, expected)
    existing = _git(repo, ["rev-parse", "--verify", "--quiet", ref], check=False)
    if not existing:
        return
    if existing != expected_commit:
        raise StateError("Git ref changed before deletion")
    completed = subprocess.run(
        ["git", "-C", str(repo), "update-ref", "-d", ref, expected_commit],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise StateError("could not delete Git ref")


def promote(
    repo: Path,
    *,
    champion_ref: str,
    candidate: str,
    expected_champion: str,
) -> None:
    candidate_commit = resolve_commit(repo, candidate)
    expected = resolve_commit(repo, expected_champion)
    completed = subprocess.run(
        ["git", "-C", str(repo), "update-ref", champion_ref, candidate_commit, expected],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        current = _git(repo, ["rev-parse", "--verify", "--quiet", champion_ref], check=False)
        if current == candidate_commit:
            return
        raise StateError("champion changed before promotion")
