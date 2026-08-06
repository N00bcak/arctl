from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from arctl.approval import confirm_approval, preview_approval, verify_approval
from arctl.cli import _approve
from arctl.errors import StateError, ValidationError
from arctl.models import TaskConfig

from .helpers import valid_task, valid_task_v4
from .test_manifest import valid_manifest


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ApprovalIntegrationTests(unittest.TestCase):
    def test_v4_locks_method_and_environment_codebase_commit(self) -> None:
        import yaml

        raw = valid_task_v4(hotseat=True)
        raw["repo"] = str(self.subject)
        raw["environment"]["codebases"][0].update(
            {
                "repo": str(self.subject),
                "commit": git(self.subject, "rev-parse", "HEAD"),
                "include": ["ENVIRONMENT.md"],
            }
        )
        raw["environment"]["probes"] = []
        raw["evaluator"] = {
            "repo": str(self.evaluator),
            "commit": self.evaluator_commit,
        }
        self.task_file.write_text(yaml.safe_dump(raw, sort_keys=False))
        task = TaskConfig.from_mapping(raw)
        preview = preview_approval(self.task_file, task)
        self.assertIsNotNone(preview.method_hash)

        confirm_approval(
            self.task_directory,
            task,
            preview,
            preview.confirmation_token,
        )

        locked = json.loads((self.task_directory / "method.lock.json").read_text())
        self.assertEqual(locked["profile"], "serial-hotseat-v1")
        self.assertEqual(verify_approval(self.task_directory, task)["champion"], git(self.subject, "rev-parse", "HEAD"))

        # The approved snapshot, not the source checkout, is authoritative.
        (self.subject / "ENVIRONMENT.md").write_text("later checkout contents\n")
        self.assertEqual(
            verify_approval(self.task_directory, task)["champion"],
            git(self.subject, "rev-parse", "HEAD"),
        )
        snapshot = self.task_directory / "environment" / "environment-core" / "ENVIRONMENT.md"
        snapshot.write_text("tampered snapshot\n")
        with self.assertRaisesRegex(StateError, "artifacts changed"):
            verify_approval(self.task_directory, task)

    def test_v4_accepts_environment_from_a_linked_git_worktree(self) -> None:
        import yaml

        environment_worktree = self.root / "environment-worktree"
        git(self.subject, "worktree", "add", "--detach", str(environment_worktree), "HEAD")
        raw = valid_task_v4()
        raw["repo"] = str(self.subject)
        raw["environment"]["codebases"][0].update(
            {
                "repo": str(environment_worktree),
                "commit": git(self.subject, "rev-parse", "HEAD"),
                "include": ["ENVIRONMENT.md"],
            }
        )
        raw["environment"]["probes"] = []
        raw["evaluator"] = {"repo": str(self.evaluator), "commit": self.evaluator_commit}
        self.task_file.write_text(yaml.safe_dump(raw, sort_keys=False))
        task = TaskConfig.from_mapping(raw)
        preview = preview_approval(self.task_file, task)
        confirm_approval(self.task_directory, task, preview, preview.confirmation_token)
        self.assertEqual(verify_approval(self.task_directory, task)["champion"], git(self.subject, "rev-parse", "HEAD"))

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.subject = self.root / "subject"
        self.evaluator = self.root / "evaluator"
        for repo in (self.subject, self.evaluator):
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "arctl tests")
            git(repo, "config", "user.email", "tests@arctl.invalid")
        (self.subject / "model.py").write_text("score = 1\n")
        (self.subject / "ENVIRONMENT.md").write_text("Public environment rules.\n")
        git(self.subject, "add", ".")
        git(self.subject, "commit", "-qm", "initial subject")
        (self.evaluator / "evaluator.manifest.json").write_text(
            json.dumps(valid_manifest(), sort_keys=True)
        )
        git(self.evaluator, "add", ".")
        git(self.evaluator, "commit", "-qm", "approved evaluator")
        self.evaluator_commit = git(self.evaluator, "rev-parse", "HEAD")
        self.task_directory = self.root / "data" / "tasks" / "demo"
        self.task_directory.mkdir(parents=True)
        self.task_file = self.task_directory / "task.yaml"
        self.task_file.write_text("exact approved task bytes\n")
        raw = valid_task()
        raw["repo"] = str(self.subject)
        raw["evaluator"] = {
            "repo": str(self.evaluator),
            "commit": self.evaluator_commit[:12],
        }
        self.raw_task = raw
        self.task = TaskConfig.from_mapping(raw)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_confirmation_locks_exact_task_manifest_and_champion(self) -> None:
        preview = preview_approval(self.task_file, self.task)
        confirm_approval(
            self.task_directory,
            self.task,
            preview,
            preview.confirmation_token,
        )
        verified = verify_approval(self.task_directory, self.task)
        self.assertEqual(verified["evaluator_commit"], self.evaluator_commit)
        champion_ref = "refs/arctl/demo/champion"
        self.assertEqual(
            git(self.subject, "rev-parse", champion_ref),
            git(self.subject, "rev-parse", "HEAD"),
        )
        self.assertEqual(
            json.loads((self.task_directory / "evaluator.manifest.json").read_text()),
            valid_manifest(),
        )
        with self.assertRaisesRegex(StateError, "already approved"):
            confirm_approval(
                self.task_directory,
                self.task,
                preview,
                preview.confirmation_token,
            )

    def test_approval_preview_summarizes_auto_protocol_and_token(self) -> None:
        import yaml

        self.task_file.write_text(yaml.safe_dump(self.raw_task, sort_keys=False))
        payload = _approve(
            data_root=self.root / "data",
            task_id="demo",
            confirmation=None,
        )
        summary = payload["approval_summary"]
        self.assertIn("Strategize: strategy-default=gpt-5.6-sol high", summary["models"])
        self.assertIn("Execute: execution-default=gpt-5.6-terra medium", summary["models"])
        self.assertEqual(summary["backends"], "codex-cli-v1 (verified)")
        self.assertEqual(summary["editable_paths"], ["src/**", "tests/**"])
        self.assertEqual(summary["environment"], "public-environment, environment-probe")
        self.assertIn("Sweep [4, 16, 64, 256]", summary["trial_count"])
        self.assertIn("seed initializes the map generator", summary["trial_seeds"])
        self.assertEqual(summary["telemetry"], {
            "higher": [],
            "lower": [],
            "contextual": [],
        })
        token = payload["approval"]["confirmation_token"]
        self.assertEqual(payload["next_command"], f"arctl approve demo --confirm {token}")

        self.raw_task["trials"] = 32
        self.task_file.write_text(yaml.safe_dump(self.raw_task, sort_keys=False))
        fixed = _approve(
            data_root=self.root / "data",
            task_id="demo",
            confirmation=None,
        )
        self.assertEqual(fixed["approval_summary"]["trial_count"], "32 paired trials.")

    def test_wrong_token_and_post_preview_change_cannot_approve(self) -> None:
        preview = preview_approval(self.task_file, self.task)
        with self.assertRaisesRegex(StateError, "token"):
            confirm_approval(self.task_directory, self.task, preview, "wrong")
        self.task_file.write_text("changed after presentation\n")
        with self.assertRaisesRegex(StateError, "changed after"):
            confirm_approval(
                self.task_directory,
                self.task,
                preview,
                preview.confirmation_token,
            )

    def test_new_automatic_task_rejects_legacy_evaluator_selected_count(
        self,
    ) -> None:
        (self.evaluator / "evaluator.manifest.json").write_text(
            json.dumps(valid_manifest(version=1), sort_keys=True)
        )
        git(self.evaluator, "add", ".")
        git(self.evaluator, "commit", "-qm", "legacy calibration")
        raw = valid_task()
        raw["repo"] = str(self.subject)
        raw["evaluator"] = {
            "repo": str(self.evaluator),
            "commit": git(self.evaluator, "rev-parse", "HEAD"),
        }
        task = TaskConfig.from_mapping(raw)
        with self.assertRaisesRegex(ValidationError, "manifest-v3"):
            preview_approval(self.task_file, task)

    def test_new_task_rejects_subject_visible_seeds(self) -> None:
        manifest = valid_manifest()
        manifest["trial"]["subject_visible_seed"] = True
        (self.evaluator / "evaluator.manifest.json").write_text(
            json.dumps(manifest, sort_keys=True)
        )
        git(self.evaluator, "add", ".")
        git(self.evaluator, "commit", "-qm", "visible seeds")
        raw = valid_task()
        raw["repo"] = str(self.subject)
        raw["evaluator"] = {
            "repo": str(self.evaluator),
            "commit": git(self.evaluator, "rev-parse", "HEAD"),
        }
        with self.assertRaisesRegex(ValidationError, "hidden trial seeds"):
            preview_approval(self.task_file, TaskConfig.from_mapping(raw))

    def test_legacy_task_contract_is_rejected(self) -> None:
        raw = valid_task()
        raw["schema_version"] = 2
        with self.assertRaisesRegex(ValidationError, "must equal 3"):
            TaskConfig.from_mapping(raw)

    def test_verification_detects_tampering(self) -> None:
        preview = preview_approval(self.task_file, self.task)
        confirm_approval(
            self.task_directory,
            self.task,
            preview,
            preview.confirmation_token,
        )
        self.task_file.chmod(0o644)
        self.task_file.write_text("tampered\n")
        with self.assertRaisesRegex(StateError, "changed"):
            verify_approval(self.task_directory, self.task)

    def test_verification_detects_environment_source_drift(self) -> None:
        preview = preview_approval(self.task_file, self.task)
        confirm_approval(
            self.task_directory,
            self.task,
            preview,
            preview.confirmation_token,
        )
        (self.subject / "ENVIRONMENT.md").write_text("Changed environment rules.\n")
        with self.assertRaisesRegex(StateError, "changed"):
            verify_approval(self.task_directory, self.task)

    def test_directory_source_hashes_only_declared_matches(self) -> None:
        package = self.subject / "environment"
        package.mkdir()
        (package / "rules.py").write_text("RULE = 1\n")
        (package / "cache.bin").write_bytes(b"generated")
        raw = dict(self.raw_task)
        raw["environment"] = {
            "sources": [
                {
                    "id": "environment-code",
                    "kind": "implementation",
                    "description": "Environment implementation.",
                    "path": "environment",
                    "include": ["**/*.py"],
                }
            ]
        }
        task = TaskConfig.from_mapping(raw)
        first = preview_approval(self.task_file, task).environment_hashes
        (package / "cache.bin").write_bytes(b"changed generated data")
        self.assertEqual(
            preview_approval(self.task_file, task).environment_hashes,
            first,
        )
        (package / "rules.py").write_text("RULE = 2\n")
        self.assertNotEqual(
            preview_approval(self.task_file, task).environment_hashes,
            first,
        )

    def test_environment_source_rejects_editable_and_private_paths(self) -> None:
        raw = dict(self.raw_task)
        raw["editable_paths"] = ["ENVIRONMENT.md"]
        with self.assertRaisesRegex(ValidationError, "overlaps editable"):
            preview_approval(self.task_file, TaskConfig.from_mapping(raw))

        raw = dict(self.raw_task)
        raw["environment"] = {
            "sources": [
                {
                    "id": "private",
                    "kind": "implementation",
                    "description": "Invalid private source.",
                    "path": str(self.evaluator / "evaluator.manifest.json"),
                }
            ]
        }
        with self.assertRaisesRegex(ValidationError, "evaluator repo"):
            preview_approval(self.task_file, TaskConfig.from_mapping(raw))

    def test_environment_source_rejects_symlinks(self) -> None:
        link = self.subject / "linked-environment.md"
        link.symlink_to(self.subject / "ENVIRONMENT.md")
        raw = dict(self.raw_task)
        raw["environment"] = {
            "sources": [
                {
                    "id": "linked",
                    "kind": "documentation",
                    "description": "Invalid linked source.",
                    "path": "linked-environment.md",
                }
            ]
        }
        with self.assertRaisesRegex(ValidationError, "symlink"):
            preview_approval(self.task_file, TaskConfig.from_mapping(raw))
