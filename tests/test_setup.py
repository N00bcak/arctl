from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arctl.errors import StateError, ValidationError
from arctl.setup import (
    QUESTION_IDS,
    SETUP_CONTROLLER_CONTRACT,
    accept_setup,
    build_setup,
    discover_setup,
    initialize_setup,
    load_setup,
    save_answers,
    setup_presentation,
    brief_changed,
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
            "schema_version": 2,
            "brief_sha256": hashlib.sha256(
                (self.workspace / "ARCTL_SETUP.md").read_bytes()
            ).hexdigest(),
            "summary": "The repository exposes one small policy.",
            "fields": [
                {
                    "id": identifier,
                    "proposed_answer": f"confirmed {identifier}",
                    "citations": [
                        {
                            "path": "README.md",
                            "location": "line 1",
                            "finding": "The public README describes the policy.",
                        }
                    ],
                    "source": "repository",
                }
                for identifier in QUESTION_IDS
            ],
            "open_questions": [],
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
            "schema_version": 2,
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
                    "path": "test_evaluator.py",
                    "content": "import unittest\n\nclass EvaluatorContractTest(unittest.TestCase):\n    def test_protocol(self):\n        self.assertTrue(True)\n",
                },
            ],
            "task": task,
            "evaluator_manifest": manifest,
        }

    def test_complete_setup_accepts_reviewed_trees_and_writes_task_v5(self) -> None:
        events: list[dict] = []
        discovered = discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", self.discovery()),
            progress=lambda event: events.append(dict(event)),
        )
        self.assertEqual(len(discovered["fields"]), len(QUESTION_IDS))
        _, record = load_setup(self.data, "demo")
        answers = {}
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
            progress=lambda event: events.append(dict(event)),
        )
        self.assertEqual(readiness["review"], "ready")
        self.assertEqual(git(self.subject, "branch", "--show-current"), "arctl/setup-demo")
        _, record = load_setup(self.data, "demo")
        task = accept_setup(self.directory, record, readiness["acceptance_token"])
        self.assertEqual(task.schema_version, 5)
        self.assertEqual(task.evaluator.commit, git(Path(record["evaluator"]), "rev-parse", "HEAD"))
        self.assertTrue((self.directory / "task.yaml").is_file())
        started = [event["stage"] for event in events if event["status"] == "started"]
        self.assertEqual(
            started,
            [
                "brief + repository discovery",
                "generation",
                "task validation",
                "dependencies",
                "evaluator checks",
                "public checks",
                "setup review",
            ],
        )

    def test_init_creates_visible_setup_brief_without_modifying_subject(self) -> None:
        brief = self.workspace / "ARCTL_SETUP.md"
        self.assertTrue(brief.is_file())
        self.assertIn("## Trial protocol", brief.read_text(encoding="utf-8"))
        self.assertFalse((self.subject / "ARCTL_SETUP.md").exists())

    def test_subject_brief_takes_precedence_and_changes_invalidate_discovery(self) -> None:
        (self.subject / "ARCTL_SETUP.md").write_text(
            "# ARCTL setup\n\n## Goal and primary outcome\nAccuracy.\n",
            encoding="utf-8",
        )
        discovery = self.discovery()
        self.assertTrue(brief_changed(self.record, discovery))

    def test_open_questions_are_grouped_and_only_those_answers_are_required(self) -> None:
        discovery = self.discovery()
        discovery["open_questions"] = [
            {
                "id": "trial_protocol",
                "prompt": "Confirm one trial protocol.",
                "why": "The brief is incomplete.",
                "proposed_answer": "Use paired independent episodes.",
                "affected_fields": ["independent_trial", "hidden_data", "randomness"],
            }
        ]
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", discovery),
        )
        _, record = load_setup(self.data, "demo")
        saved = save_answers(
            self.directory,
            record,
            {"trial_protocol": "Use paired fresh episodes."},
            {"runtime_budget": "Five minutes per batch."},
        )
        self.assertEqual(set(saved["answers"]), {"trial_protocol"})
        self.assertEqual(set(saved["overrides"]), {"runtime_budget"})
        resolved = json.loads(
            (self.directory / "setup" / "resolved.public.json").read_text()
        )
        by_id = {item["id"]: item["resolved_answer"] for item in resolved["fields"]}
        self.assertIn("Human clarification: Use paired fresh episodes.", by_id["randomness"])
        self.assertEqual(by_id["runtime_budget"], "Five minutes per batch.")

    def test_discovery_receives_fixed_controller_rules_and_no_guessing_instruction(self) -> None:
        prompts: list[str] = []

        def builder(worktree: Path, scratch: Path, schema: Path, prompt: str):
            prompts.append(prompt)
            return output_builder("discovery.public.json", self.discovery())(
                worktree, scratch, schema, prompt
            )

        discover_setup(self.directory, self.record, command_builder=builder)
        self.assertIn("controller_owned_contract", prompts[0])
        self.assertIn("Do not invent numeric limits", prompts[0])
        self.assertIn("Positive lower bound accepts", SETUP_CONTROLLER_CONTRACT["decision"])

    def test_unsupported_clarification_has_no_acceptable_empty_default(self) -> None:
        discovery = self.discovery()
        discovery["open_questions"] = [
            {
                "id": "evaluator",
                "prompt": "Which uncertainty method should be used?",
                "why": "Neither brief nor repository specifies one.",
                "proposed_answer": None,
                "affected_fields": ["evaluator_pattern"],
            }
        ]
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", discovery),
        )
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(ValidationError, "every open clarification"):
            save_answers(self.directory, record, {})

    def test_v1_discovery_consolidates_repeated_pairing_questions(self) -> None:
        old = {
            "schema_version": 1,
            "summary": "old",
            "questions": [
                {
                    "id": identifier,
                    "prompt": f"Confirm {identifier}",
                    "why": "old contract",
                    "proposed_answer": "Humans must confirm paired trials.",
                    "citations": [],
                }
                for identifier in QUESTION_IDS
            ],
        }
        presentation = setup_presentation(old)
        groups = [item["id"] for item in presentation["open_questions"]]
        self.assertEqual(
            groups, ["target", "trial_protocol", "constraints", "evaluator"]
        )

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

    def test_invalid_contract_is_repaired_once_before_dependencies(self) -> None:
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", self.discovery()),
        )
        _, record = load_setup(self.data, "demo")
        save_answers(self.directory, record, {})
        stale = Path(record["evaluator"]) / "manifest.json"
        stale.write_text("{}\n", encoding="utf-8")
        old_output = (
            self.directory
            / "setup"
            / "build"
            / "attempts"
            / "0000"
            / "output"
        )
        old_output.mkdir(parents=True)
        (old_output / "build.public.json").write_text(
            json.dumps({"evaluator_files": [{"path": "manifest.json"}]}),
            encoding="utf-8",
        )
        values = [
            {
                **self.build_value(),
                "task": {"schema_version": 5, "subject": {}},
                "evaluator_manifest": {"schema_version": 3, "name": "obsolete"},
            },
            self.build_value(),
        ]
        prompts: list[str] = []

        def builder(worktree: Path, scratch: Path, schema: Path, prompt: str):
            prompts.append(prompt)
            return output_builder("build.public.json", values.pop(0))(
                worktree, scratch, schema, prompt
            )

        events: list[dict] = []
        _, record = load_setup(self.data, "demo")
        readiness = build_setup(
            self.directory,
            record,
            offline=False,
            command_builder=builder,
            review_command_builder=output_builder(
                "review.public.json",
                {"schema_version": 1, "summary": "No findings.", "findings": []},
            ),
            progress=lambda event: events.append(dict(event)),
        )
        self.assertEqual(readiness["review"], "ready")
        self.assertIn("task contract:", prompts[1])
        self.assertIn("evaluator manifest contract:", prompts[1])
        stages = [event["stage"] for event in events]
        self.assertLess(stages.index("contract repair"), stages.index("dependencies"))
        self.assertFalse(stale.exists())

    def test_invalid_contract_after_repair_reports_exact_error(self) -> None:
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", self.discovery()),
        )
        _, record = load_setup(self.data, "demo")
        save_answers(self.directory, record, {})
        invalid = {
            **self.build_value(),
            "task": {},
            "evaluator_manifest": {},
        }
        events: list[dict] = []
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(
            StateError,
            "invalid after one repair: initial validation: task contract: "
            "task fields differ.*repair: task contract:.*evaluator manifest contract",
        ):
            build_setup(
                self.directory,
                record,
                offline=False,
                command_builder=output_builder("build.public.json", invalid),
                progress=lambda event: events.append(dict(event)),
            )
        self.assertNotIn("dependencies", [event["stage"] for event in events])
