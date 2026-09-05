from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arctl.errors import ResearchMiss, StateError
from arctl.git import (
    create_candidate_commit,
    create_candidate_ref,
    create_detached_worktree,
    ensure_clean_worktree,
    normalize_runtime_artifacts,
    promote,
    remove_worktree,
    resolve_commit,
    validate_candidate,
)


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class GitIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.audit_temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.audit_root = Path(self.audit_temporary.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "arctl tests")
        git(self.repo, "config", "user.email", "tests@arctl.invalid")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "model.py").write_text("score = 1\n")
        (self.repo / "README.md").write_text("subject\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "champion")
        self.champion = resolve_commit(self.repo, "HEAD")
        self.champion_ref = "refs/arctl/demo/champion"
        git(self.repo, "update-ref", self.champion_ref, self.champion)

    def tearDown(self) -> None:
        self.temporary.cleanup()
        self.audit_temporary.cleanup()

    def commit(self, path: str, content: str) -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git(self.repo, "add", path)
        git(self.repo, "commit", "-qm", f"change {path}")
        return resolve_commit(self.repo, "HEAD")

    def isolated_import(self, relative: str = "src/model.py") -> Path:
        cache = self.audit_root / "redirected-pycache"
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                (
                    "import importlib.util; "
                    f"spec = importlib.util.spec_from_file_location('module', {relative!r}); "
                    "module = importlib.util.module_from_spec(spec); "
                    "spec.loader.exec_module(module)"
                ),
            ],
            cwd=self.repo,
            env={**os.environ, "PYTHONPYCACHEPREFIX": str(cache)},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        pycache = self.repo / Path(relative).parent / "__pycache__"
        artifacts = list(pycache.glob("*.pyc"))
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(list(cache.rglob("*.pyc")), [])
        return artifacts[0]

    def test_validates_single_parent_candidate_and_promotes_with_cas(self) -> None:
        candidate = self.commit("src/model.py", "score = 2\n")
        paths = validate_candidate(
            self.repo,
            champion=self.champion,
            candidate=candidate,
            editable_paths=("src/**",),
            denied_paths=(".git/**", "pyproject.toml"),
        )
        self.assertEqual(paths, ("src/model.py",))
        promote(
            self.repo,
            champion_ref=self.champion_ref,
            candidate=candidate,
            expected_champion=self.champion,
        )
        self.assertEqual(resolve_commit(self.repo, self.champion_ref), candidate)

    def test_rejects_denied_and_out_of_scope_paths(self) -> None:
        candidate = self.commit("README.md", "tampered\n")
        with self.assertRaisesRegex(StateError, "outside editable"):
            validate_candidate(
                self.repo,
                champion=self.champion,
                candidate=candidate,
                editable_paths=("src/**",),
                denied_paths=("pyproject.toml",),
            )

    def test_globstar_covers_nested_paths(self) -> None:
        candidate = self.commit("src/nested/model.py", "score = 2\n")
        self.assertEqual(
            validate_candidate(
                self.repo,
                champion=self.champion,
                candidate=candidate,
                editable_paths=("src/**",),
                denied_paths=(),
            ),
            ("src/nested/model.py",),
        )

    def test_globstar_denial_covers_nested_paths(self) -> None:
        candidate = self.commit("src/private/nested/key.py", "secret = True\n")
        with self.assertRaisesRegex(StateError, "denied path"):
            validate_candidate(
                self.repo,
                champion=self.champion,
                candidate=candidate,
                editable_paths=("src/**",),
                denied_paths=("src/private/**",),
            )

    def test_rejects_unchanged_tree(self) -> None:
        git(self.repo, "commit", "--allow-empty", "-qm", "empty candidate")
        candidate = resolve_commit(self.repo, "HEAD")
        with self.assertRaisesRegex(StateError, "tree is unchanged"):
            validate_candidate(
                self.repo,
                champion=self.champion,
                candidate=candidate,
                editable_paths=("src/**",),
                denied_paths=(),
            )

    def test_rejects_candidate_not_based_on_frozen_champion(self) -> None:
        intermediate = self.commit("src/model.py", "score = 2\n")
        candidate = self.commit("src/model.py", "score = 3\n")
        self.assertNotEqual(intermediate, candidate)
        with self.assertRaisesRegex(StateError, "approved champion"):
            validate_candidate(
                self.repo,
                champion=self.champion,
                candidate=candidate,
                editable_paths=("src/**",),
                denied_paths=(),
            )

    def test_compare_and_swap_refuses_stale_promotion(self) -> None:
        candidate = self.commit("src/model.py", "score = 2\n")
        git(self.repo, "update-ref", self.champion_ref, candidate, self.champion)
        with self.assertRaisesRegex(StateError, "changed before promotion"):
            promote(
                self.repo,
                champion_ref=self.champion_ref,
                candidate=self.champion,
                expected_champion=self.champion,
            )

    def test_clean_worktree_check_catches_tracked_and_untracked_changes(self) -> None:
        ensure_clean_worktree(self.repo)
        (self.repo / "src" / "model.py").write_text("dirty = True\n")
        with self.assertRaisesRegex(StateError, "not clean"):
            ensure_clean_worktree(self.repo)
        git(self.repo, "restore", "src/model.py")
        (self.repo / "untracked.txt").write_text("untracked\n")
        with self.assertRaisesRegex(StateError, "not clean"):
            ensure_clean_worktree(self.repo)

    def test_controller_creates_candidate_commit_without_moving_research_head(self) -> None:
        (self.repo / "src" / "model.py").write_text("score = 2\n")
        candidate, paths = create_candidate_commit(
            self.repo,
            champion=self.champion,
            editable_paths=("src/**",),
            denied_paths=(),
            prior_candidate_ref_prefix="refs/arctl/demo/candidates/",
            message="arctl experiment 1",
        )
        self.assertEqual(resolve_commit(self.repo, "HEAD"), self.champion)
        self.assertEqual(paths, ("src/model.py",))
        self.assertEqual(
            git(self.repo, "show", "-s", "--format=%P", candidate),
            self.champion,
        )
        ref = "refs/arctl/demo/candidates/000001"
        create_candidate_ref(self.repo, ref, candidate)
        create_candidate_ref(self.repo, ref, candidate)
        self.assertEqual(resolve_commit(self.repo, ref), candidate)

        with self.assertRaisesRegex(ResearchMiss, "already tested"):
            create_candidate_commit(
                self.repo,
                champion=self.champion,
                editable_paths=("src/**",),
                denied_paths=(),
                prior_candidate_ref_prefix="refs/arctl/demo/candidates/",
                message="duplicate",
            )

    def test_controller_discards_isolated_python_cache_before_staging(self) -> None:
        source = self.repo / "src" / "model.py"
        source.write_text("score = 2\n")
        self.isolated_import()

        audit = self.audit_root / "runtime-artifacts.public.json"
        candidate, paths = create_candidate_commit(
            self.repo,
            champion=self.champion,
            editable_paths=("src/**",),
            denied_paths=(),
            prior_candidate_ref_prefix="refs/arctl/demo/candidates/",
            message="isolated Python candidate",
            runtime_artifact_audit=audit,
        )
        self.assertEqual(paths, ("src/model.py",))
        self.assertEqual(
            git(self.repo, "diff", "--name-only", self.champion, candidate),
            "src/model.py",
        )
        self.assertFalse((self.repo / "src" / "__pycache__").exists())
        self.assertEqual(
            json.loads(audit.read_text(encoding="utf-8")),
            {
                "events": [
                    {
                        "stage": "candidate-staging",
                        "discarded_paths": [
                            f"src/__pycache__/model.{sys.implementation.cache_tag}.pyc"
                        ],
                    }
                ],
            },
        )

    def test_runtime_artifact_cleanup_is_recovery_idempotent(self) -> None:
        artifact = self.isolated_import()
        relative = artifact.relative_to(self.repo).as_posix()
        audit = self.audit_root / "runtime-artifacts.public.json"

        self.assertEqual(
            normalize_runtime_artifacts(
                self.repo, stage="implementation", audit_path=audit
            ),
            (relative,),
        )
        self.assertFalse(artifact.exists())
        recreated = self.isolated_import()
        self.assertEqual(recreated.relative_to(self.repo).as_posix(), relative)
        self.assertEqual(
            normalize_runtime_artifacts(
                self.repo, stage="implementation", audit_path=audit
            ),
            (relative,),
        )
        self.assertFalse(recreated.exists())
        self.assertEqual(
            len(json.loads(audit.read_text(encoding="utf-8"))["events"]),
            1,
        )

    def test_runtime_artifact_cleanup_includes_gitignored_caches(self) -> None:
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        artifact = self.isolated_import()

        discarded = normalize_runtime_artifacts(
            self.repo,
            stage="implementation",
            audit_path=self.audit_root / "runtime-artifacts.public.json",
        )

        self.assertEqual(discarded, (artifact.relative_to(self.repo).as_posix(),))
        self.assertFalse(artifact.exists())

    def test_runtime_artifact_cleanup_rejects_an_invalid_or_local_audit(self) -> None:
        audit = self.audit_root / "runtime-artifacts.public.json"
        audit.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(StateError, "audit is invalid"):
            normalize_runtime_artifacts(
                self.repo, stage="implementation", audit_path=audit
            )
        with self.assertRaisesRegex(ValueError, "outside the candidate worktree"):
            normalize_runtime_artifacts(
                self.repo,
                stage="implementation",
                audit_path=self.repo / "runtime-artifacts.public.json",
            )

    def test_runtime_artifact_cleanup_does_not_mask_other_scope_violations(self) -> None:
        (self.repo / "src" / "model.py").write_text("score = 2\n")
        self.isolated_import()
        (self.repo / "README.md").write_text("tampered\n")

        with self.assertRaisesRegex(ResearchMiss, "outside editable.*README.md"):
            create_candidate_commit(
                self.repo,
                champion=self.champion,
                editable_paths=("src/model.py",),
                denied_paths=(),
                prior_candidate_ref_prefix="refs/arctl/demo/candidates/",
                message="invalid candidate",
                runtime_artifact_audit=(
                    self.audit_root / "runtime-artifacts.public.json"
                ),
            )

    def test_runtime_artifact_cleanup_rejects_noncanonical_bytecode(self) -> None:
        cases = (
            "src/model.pyc",
            f"src/__pycache__/orphan.{sys.implementation.cache_tag}.pyc",
        )
        for relative in cases:
            with self.subTest(relative=relative):
                target = self.repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"not a disposable cache")
                with self.assertRaisesRegex(ResearchMiss, "outside editable"):
                    create_candidate_commit(
                        self.repo,
                        champion=self.champion,
                        editable_paths=("src/model.py",),
                        denied_paths=(),
                        prior_candidate_ref_prefix="refs/arctl/demo/candidates/",
                        message="invalid bytecode candidate",
                    )
                git(self.repo, "reset", "-q", "HEAD")
                target.unlink()

    def test_runtime_artifact_cleanup_rejects_tracked_or_symlinked_cache(self) -> None:
        relative = f"src/__pycache__/model.{sys.implementation.cache_tag}.pyc"
        cache = self.repo / relative
        cache.parent.mkdir()
        cache.write_bytes(b"tracked cache")
        git(self.repo, "add", relative)
        git(self.repo, "commit", "-qm", "tracked cache champion")
        self.champion = resolve_commit(self.repo, "HEAD")
        cache.write_bytes(b"modified tracked cache")
        with self.assertRaisesRegex(ResearchMiss, "outside editable"):
            create_candidate_commit(
                self.repo,
                champion=self.champion,
                editable_paths=("src/model.py",),
                denied_paths=(),
                prior_candidate_ref_prefix="refs/arctl/demo/candidates/",
                message="tracked cache candidate",
            )

        git(self.repo, "reset", "--hard", "-q", self.champion)
        git(self.repo, "rm", "-q", relative)
        git(self.repo, "commit", "-qm", "remove tracked cache")
        self.champion = resolve_commit(self.repo, "HEAD")
        cache.parent.mkdir(exist_ok=True)
        cache.symlink_to(self.repo / "src" / "model.py")
        with self.assertRaisesRegex(ResearchMiss, "outside editable"):
            create_candidate_commit(
                self.repo,
                champion=self.champion,
                editable_paths=("src/model.py",),
                denied_paths=(),
                prior_candidate_ref_prefix="refs/arctl/demo/candidates/",
                message="symlink cache candidate",
            )

    def test_detached_worktree_creation_and_removal(self) -> None:
        worktree = self.repo.parent / "detached"
        create_detached_worktree(self.repo, worktree, self.champion)
        self.assertEqual(resolve_commit(worktree, "HEAD"), self.champion)
        remove_worktree(self.repo, worktree)
        self.assertFalse(worktree.exists())

    def test_recreates_missing_but_registered_worktree(self) -> None:
        worktree = self.repo.parent / "detached"
        create_detached_worktree(self.repo, worktree, self.champion)
        shutil.rmtree(worktree)

        create_detached_worktree(self.repo, worktree, self.champion)

        self.assertEqual(resolve_commit(worktree, "HEAD"), self.champion)
        remove_worktree(self.repo, worktree)
