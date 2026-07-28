"""Git operations whose invariants are owned by the controller."""

from __future__ import annotations

import subprocess
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Sequence

from .errors import StateError


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


def resolve_commit(repo: Path, revision: str) -> str:
    return _git(repo, ["rev-parse", "--verify", f"{revision}^{{commit}}"])


def ensure_clean_worktree(repo: Path) -> None:
    if _git(repo, ["status", "--porcelain", "--untracked-files=all"]):
        raise StateError(f"Git worktree is not clean: {repo}")


def candidate_changed_paths(repo: Path, champion: str, candidate: str) -> tuple[str, ...]:
    output = _git(repo, ["diff", "--name-only", "-z", champion, candidate])
    return tuple(path for path in output.split("\0") if path)


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
    _git(repo, ["worktree", "add", "--detach", str(path), resolve_commit(repo, revision)])
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
) -> tuple[str, tuple[str, ...]]:
    champion_commit = resolve_commit(worktree, champion)
    if resolve_commit(worktree, "HEAD") != champion_commit:
        raise StateError("research worktree no longer points at the starting champion")
    _git(worktree, ["add", "--all"])
    output = _git(worktree, ["diff", "--cached", "--name-only", "-z", champion_commit])
    paths = tuple(path for path in output.split("\0") if path)
    if not paths:
        raise StateError("candidate tree is unchanged")
    validate_changed_paths(
        paths,
        editable_paths=editable_paths,
        denied_paths=denied_paths,
    )
    tree = _git(worktree, ["write-tree"])
    if tree == _git(worktree, ["rev-parse", f"{champion_commit}^{{tree}}"]):
        raise StateError("candidate tree is unchanged")

    refs = _git(
        worktree,
        ["for-each-ref", "--format=%(objectname)", prior_candidate_ref_prefix],
    ).splitlines()
    for prior in refs:
        parents = _git(worktree, ["show", "-s", "--format=%P", prior]).split()
        if parents == [champion_commit] and _git(
            worktree, ["rev-parse", f"{prior}^{{tree}}"]
        ) == tree:
            raise StateError(
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
