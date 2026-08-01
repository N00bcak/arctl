from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from arctl.approval import confirm_approval, preview_approval, verify_approval
from arctl.errors import StateError, ValidationError
from arctl.models import TaskConfig

from .helpers import valid_task
from .test_manifest import valid_manifest


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ApprovalIntegrationTests(unittest.TestCase):
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
