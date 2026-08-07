from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from arctl.errors import StateError
from arctl.setup import (
    QUESTION_IDS,
    accept_setup,
    build_setup,
    discover_setup,
    initialize_setup,
    load_setup,
    save_answers,
)

from .test_manifest import valid_manifest


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def output_builder(name: str, value: dict):
    def build(_worktree: Path, scratch: Path, _schema: Path, _prompt: str):
        script = (
            "import pathlib,sys; "
            "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
        )
        return (
            sys.executable,
            "-c",
            script,
            str(scratch / name),
            json.dumps(value),
        )

    return build


class GuidedSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.subject = self.root / "subject"
        self.subject.mkdir()
        git(self.subject, "init", "-q")
        git(self.subject, "config", "user.name", "tests")
        git(self.subject, "config", "user.email", "tests@invalid")
        (self.subject / "README.md").write_text("# Demo policy\n")
        git(self.subject, "add", ".")
        git(self.subject, "commit", "-qm", "baseline")
        self.workspace = self.root / "demo-research"
        self.data = self.workspace / ".arctl-data"
        initialize_setup(
            data_root=self.data,
            workspace=self.workspace,
            repo=self.subject,
            new_repo=False,
            task_id="demo",
        )
        self.directory, self.record = load_setup(self.data, "demo")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def discovery(self) -> dict:
        return {
            "schema_version": 1,
            "summary": "The repository exposes one small policy.",
            "questions": [
                {
                    "id": identifier,
                    "prompt": f"Confirm {identifier}.",
                    "why": "The setup contract requires it.",
                    "proposed_answer": f"confirmed {identifier}",
                    "citations": [
                        {
                            "path": "README.md",
                            "location": "line 1",
                            "finding": "The public README describes the policy.",
                        }
                    ],
                }
                for identifier in QUESTION_IDS
            ],
        }

    def build_value(self) -> dict:
        python = self.workspace / ".venv" / "bin" / "python"
        manifest = valid_manifest()
        task = {
            "schema_version": 5,
            "task_id": "demo",
            "repo": str(self.subject),
            "objective": "Improve the demo policy.",
            "editable_paths": ["solution/**"],
            "denied_paths": [".git/**", "README.md"],
            "public_checks": [[str(python), "-c", "pass"]],
            "public_probe": {
                "command": [str(python), "-c", "pass"],
                "trial_equivalents": 1,
            },
            "environment": {
                "codebases": [
                    {
                        "id": "subject-interface",
                        "description": "Public policy interface.",
                        "repo": str(self.subject),
                        "commit": "SETUP_SUBJECT_COMMIT",
                        "include": ["README.md"],
                    },
                    {
                        "id": "environment-core",
                        "description": "Public rules.",
                        "repo": self.record["environment"],
                        "commit": "SETUP_ENVIRONMENT_COMMIT",
                        "include": ["README.md"],
                    }
                ],
                "probes": [],
            },
            "evaluator": {
                "repo": self.record["evaluator"],
                "commit": "SETUP_EVALUATOR_COMMIT",
            },
            "method": {
                "profile": "serial-v1",
                "allow_unverified_isolation": False,
            },
            "trials": 1,
            "max_experiments": 10,
        }
        return {
            "schema_version": 1,
            "summary": "Generated a minimal Python task.",
            "dependencies": [],
            "subject_files": [
                {"path": "solution/policy.py", "content": "VALUE = 1\n"}
            ],
            "environment_files": [
                {"path": "README.md", "content": "Public environment rules.\n"}
            ],
            "evaluator_files": [
                {
                    "path": "evaluator.manifest.json",
                    "content": json.dumps(manifest),
                },
                {
                    "path": "test_evaluator.py",
                    "content": "import unittest\n\nclass EvaluatorContractTest(unittest.TestCase):\n    def test_protocol(self):\n        self.assertTrue(True)\n",
                },
            ],
            "task_yaml": yaml.safe_dump(task, sort_keys=False),
        }

    def test_complete_setup_accepts_reviewed_trees_and_writes_task_v5(self) -> None:
        discovered = discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", self.discovery()),
        )
        self.assertEqual(len(discovered["questions"]), len(QUESTION_IDS))
        _, record = load_setup(self.data, "demo")
        answers = {
            item["id"]: item["proposed_answer"] for item in discovered["questions"]
        }
        save_answers(self.directory, record, answers)
        _, record = load_setup(self.data, "demo")
        readiness = build_setup(
            self.directory,
            record,
            offline=False,
            command_builder=output_builder("build.public.json", self.build_value()),
            review_command_builder=output_builder(
                "review.public.json",
                {"schema_version": 1, "summary": "No findings.", "findings": []},
            ),
        )
        self.assertEqual(readiness["review"], "ready")
        self.assertEqual(git(self.subject, "branch", "--show-current"), "arctl/setup-demo")
        _, record = load_setup(self.data, "demo")
        task = accept_setup(self.directory, record, readiness["acceptance_token"])
        self.assertEqual(task.schema_version, 5)
        self.assertEqual(task.evaluator.commit, git(Path(record["evaluator"]), "rev-parse", "HEAD"))
        self.assertTrue((self.directory / "task.yaml").is_file())

    def test_acceptance_rejects_changes_after_review(self) -> None:
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", self.discovery()),
        )
        _, record = load_setup(self.data, "demo")
        save_answers(
            self.directory,
            record,
            {identifier: f"answer {identifier}" for identifier in QUESTION_IDS},
        )
        _, record = load_setup(self.data, "demo")
        readiness = build_setup(
            self.directory,
            record,
            offline=False,
            command_builder=output_builder("build.public.json", self.build_value()),
            review_command_builder=output_builder(
                "review.public.json",
                {"schema_version": 1, "summary": "No findings.", "findings": []},
            ),
        )
        (self.subject / "solution" / "policy.py").write_text("VALUE = 2\n")
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(StateError, "changed after review"):
            accept_setup(self.directory, record, readiness["acceptance_token"])

    def test_failed_review_can_be_rebuilt_in_a_new_immutable_attempt(self) -> None:
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", self.discovery()),
        )
        _, record = load_setup(self.data, "demo")
        save_answers(
            self.directory,
            record,
            {identifier: f"answer {identifier}" for identifier in QUESTION_IDS},
        )
        _, record = load_setup(self.data, "demo")
        first = build_setup(
            self.directory,
            record,
            offline=False,
            command_builder=output_builder("build.public.json", self.build_value()),
            review_command_builder=output_builder(
                "review.public.json",
                {
                    "schema_version": 1,
                    "summary": "One defect.",
                    "findings": [
                        {
                            "code": "SEED",
                            "location": "adapter",
                            "message": "Fix seed handling.",
                        }
                    ],
                },
            ),
        )
        self.assertEqual(first["review"], "blocked")
        _, record = load_setup(self.data, "demo")
        second = build_setup(
            self.directory,
            record,
            offline=False,
            command_builder=output_builder("build.public.json", self.build_value()),
            review_command_builder=output_builder(
                "review.public.json",
                {"schema_version": 1, "summary": "Repaired.", "findings": []},
            ),
        )
        self.assertEqual(second["review"], "ready")
        attempts = self.directory / "setup" / "review" / "attempts"
        self.assertEqual(
            sorted(path.name for path in attempts.iterdir()), ["0001", "0002"]
        )
