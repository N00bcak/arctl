from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from arctl.errors import StateError
from arctl.sandbox import MAX_AGENT_PROMPT_BYTES
from arctl.setup import (
    QUESTION_IDS,
    SETUP_CONTROLLER_CONTRACT,
    accept_setup,
    build_schema,
    build_setup,
    discover_setup,
    initialize_setup,
    load_setup,
    review_setup_edits,
    save_answers,
    setup_presentation,
    brief_changed,
    _build_semantic_findings,
    _agent_failure_detail,
    _upgrade_build_v3,
    _validate_build_contract,
    _run_setup_command,
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
            "schema_version": 5,
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
            "capability_downgrades": [],
        }

    def build_value(self) -> dict:
        manifest = valid_manifest()
        task = {
            "schema_version": 5,
            "task_id": "demo",
            "repo": str(self.subject),
            "objective": "Improve the demo policy.",
            "editable_paths": ["solution/**"],
            "denied_paths": [".git/**", "README.md"],
            "public_checks": [
                {"kind": "module", "target": "compileall", "arguments": ["-q", "."]}
            ],
            "public_probe": {
                "execution": {
                    "kind": "module", "target": "compileall", "arguments": ["-q", "."]
                },
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
        for controller_field in (
            "schema_version",
            "task_id",
            "repo",
            "evaluator",
            "method",
        ):
            task.pop(controller_field)
        manifest["schemas"] = {
            "public_case_json": json.dumps(manifest["schemas"]["public_case"]),
            "subject_result_json": json.dumps(manifest["schemas"]["subject_result"]),
        }
        manifest["public"]["telemetry"] = [
            {"name": name, **metric}
            for name, metric in manifest["public"]["telemetry"].items()
        ]
        for field in (
            "schema_version",
            "subject_command",
            "prepare_command",
            "calibrate_command",
            "score_command",
        ):
            manifest.pop(field)
        for source in task["environment"]["codebases"]:
            source["owner"] = (
                "subject" if source.pop("repo") == str(self.subject) else "environment"
            )
            source.pop("commit")
        return {
            "schema_version": 4,
            "summary": "Generated a minimal Python task.",
            "dependencies": [],
            "subject_hook": (
                "def run_batch(public_batch):\n"
                "    return {'schema_version': 1, "
                "'trial_count': public_batch['trial_count'], "
                "'results': [{'score': float(case['value'])} "
                "for case in public_batch['cases']]}\n"
            ),
            "evaluator_hook": (
                "def prepare(context):\n"
                "    return {'public_batch': {'schema_version': 1, "
                "'trial_count': context['trial_count'], "
                "'cases': [{'value': seed} for seed in context['trial_seeds']]}, "
                "'private_scoring': {}}\n\n"
                "def calibrate(context):\n"
                "    return [{'trial_count': count, 'diagnostic_value': 0.0} "
                "for count in context['ladder']]\n\n"
                "def score(context):\n"
                "    candidate = context['candidate_output']['results']\n"
                "    champion = context['champion_output']['results']\n"
                "    effect = sum(b['score'] - a['score'] for a, b in "
                "zip(champion, candidate)) / len(candidate)\n"
                "    return {'hard_rules_pass': True, 'effect_estimate': effect, "
                "'one_sided_lower_bound': effect, 'suspect_required': False, "
                "'suspect_reason': None, 'telemetry': {}}\n"
            ),
            "evaluator_test": (
                "import unittest\nfrom _arctl.hook import prepare\n\n"
                "class HookTest(unittest.TestCase):\n"
                "    def test_prepare(self):\n"
                "        context = {'trial_seeds': [1], 'trial_count': 1}\n"
                "        self.assertIn('public_batch', prepare(context))\n"
            ),
            "subject_files": [
                {"path": "solution/policy.py", "content": "VALUE = 1\n"}
            ],
            "environment_files": [
                {"path": "README.md", "content": "Public environment rules.\n"}
            ],
            "evaluator_files": [],
            "task": task,
            "evaluator": manifest,
        }

    def ready_setup(self) -> dict:
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", self.discovery()),
        )
        _, record = load_setup(self.data, "demo")
        save_answers(self.directory, record, {})
        _, record = load_setup(self.data, "demo")
        return build_setup(
            self.directory,
            record,
            offline=False,
            command_builder=output_builder("build.public.json", self.build_value()),
            review_command_builder=output_builder(
                "review.public.json",
                {"schema_version": 1, "summary": "No findings.", "findings": []},
            ),
        )

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
        self.assertEqual(git(self.subject, "branch", "--show-current"), "master")
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
                "protocol preflight",
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
                "id": "constraints",
                "prompt": "Confirm the practical resource ceiling.",
                "why": "The brief is incomplete.",
                "proposed_answer": "Use five minutes per comparison.",
                "affected_fields": ["hard_rules", "runtime_budget"],
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
            {"constraints": "Use ten minutes per comparison."},
            {"runtime_budget": "Five minutes per batch."},
        )
        self.assertEqual(set(saved["answers"]), {"constraints"})
        self.assertEqual(set(saved["overrides"]), {"runtime_budget"})
        resolved = json.loads(
            (self.directory / "setup" / "resolved.public.json").read_text()
        )
        by_id = {item["id"]: item["resolved_answer"] for item in resolved["fields"]}
        self.assertIn(
            "Human clarification: Use ten minutes per comparison.",
            by_id["hard_rules"],
        )
        self.assertEqual(by_id["runtime_budget"], "Five minutes per batch.")

    def test_one_constraints_question_may_cover_every_affected_field(self) -> None:
        discovery = self.discovery()
        discovery["open_questions"] = [
            {
                "id": "constraints",
                "prompt": "Accept the enforceable resource boundary?",
                "why": "The requested limits exceed controller capabilities.",
                "proposed_answer": "Accept the documented limits.",
                "affected_fields": ["hard_rules", "telemetry", "runtime_budget"],
            }
        ]
        discovery["capability_downgrades"] = [
            {"capability_id": "comparison_deadline"},
            {"capability_id": "memory_limit"},
        ]
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", discovery),
        )
        _, record = load_setup(self.data, "demo")
        save_answers(self.directory, record, {"constraints": "Accepted."})
        resolved = json.loads(
            (self.directory / "setup" / "resolved.public.json").read_text()
        )
        by_id = {item["id"]: item["resolved_answer"] for item in resolved["fields"]}
        for identifier in ("hard_rules", "telemetry", "runtime_budget"):
            self.assertIn("Human clarification: Accepted.", by_id[identifier])

    def test_discovery_receives_fixed_controller_rules_and_no_guessing_instruction(self) -> None:
        prompts: list[str] = []

        def builder(worktree: Path, scratch: Path, schema: Path, prompt: str):
            prompts.append(prompt)
            return output_builder("discovery.public.json", self.discovery())(
                worktree, scratch, schema, prompt
            )

        discover_setup(self.directory, self.record, command_builder=builder)
        self.assertIn("controller_owned_contract", prompts[0])
        self.assertIn("Never ask the human for a sampling distribution", prompts[0])
        self.assertIn("inspect the environment implementation", prompts[0])
        self.assertIn("Positive lower bound accepts", SETUP_CONTROLLER_CONTRACT["decision"])

    def test_specialist_protocol_question_is_rejected_by_schema(self) -> None:
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
        with self.assertRaisesRegex(StateError, "invalid discovery.public.json"):
            discover_setup(
                self.directory,
                self.record,
                command_builder=output_builder("discovery.public.json", discovery),
            )

    def test_discovery_v2_is_invalidated_for_specialist_owned_protocols(self) -> None:
        discovery = self.discovery()
        discovery["schema_version"] = 2
        self.assertTrue(brief_changed(self.record, discovery))

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
        staged_subject = Path(readiness["staging"]["subject"])
        (staged_subject / "solution" / "policy.py").write_text("VALUE = 2\n")
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(StateError, "changed after review"):
            accept_setup(self.directory, record, readiness["acceptance_token"])

    def test_acceptance_preserves_unrelated_unstaged_changes(self) -> None:
        readiness = self.ready_setup()
        unrelated = self.subject / "notes.txt"
        unrelated.write_text("keep me\n", encoding="utf-8")
        _, record = load_setup(self.data, "demo")
        accept_setup(self.directory, record, readiness["acceptance_token"])
        committed = git(self.subject, "show", "--pretty=format:", "--name-only", "HEAD")
        self.assertNotIn("notes.txt", committed.splitlines())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me\n")

    def test_acceptance_rejects_a_preexisting_staged_change(self) -> None:
        readiness = self.ready_setup()
        (self.subject / "README.md").write_text("staged user edit\n", encoding="utf-8")
        git(self.subject, "add", "README.md")
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(StateError, "empty Git index"):
            accept_setup(self.directory, record, readiness["acceptance_token"])
        self.assertIn("README.md", git(self.subject, "diff", "--cached", "--name-only"))

    def test_acceptance_token_covers_the_owned_file_list(self) -> None:
        readiness = self.ready_setup()
        readiness_path = self.directory / "setup" / "readiness.public.json"
        readiness["owned_files"]["subject_files"].append(
            {"path": "unreviewed.py", "content": "unreviewed\n"}
        )
        readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(StateError, "rerun setup to review the edits"):
            accept_setup(self.directory, record, readiness["acceptance_token"])

    def test_task_edit_requires_review_and_issues_a_new_token(self) -> None:
        readiness = self.ready_setup()
        old_token = readiness["acceptance_token"]
        task_path = self.directory / "task.draft.yaml"
        task_value = yaml.safe_load(task_path.read_text(encoding="utf-8"))
        task_value["objective"] = "Improve the reviewed demo policy safely."
        task_path.write_text(yaml.safe_dump(task_value, sort_keys=False), encoding="utf-8")
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(StateError, "rerun setup to review the edits"):
            accept_setup(self.directory, record, old_token)
        _, record = load_setup(self.data, "demo")
        updated = review_setup_edits(
            self.directory,
            record,
            offline=False,
            review_command_builder=output_builder(
                "review.public.json",
                {"schema_version": 1, "summary": "No findings.", "findings": []},
            ),
        )
        self.assertNotEqual(updated["acceptance_token"], old_token)
        change = json.loads(
            next((self.directory / "setup" / "edits" / "attempts").glob("*/change.public.json")).read_text()
        )
        self.assertEqual(change["task_fields"], ["objective"])
        self.assertEqual(change["stages"], ["setup review"])

    def test_environment_edit_reruns_only_validation_and_review(self) -> None:
        readiness = self.ready_setup()
        environment = Path(readiness["staging"]["environment"])
        (environment / "README.md").write_text(
            "Revised public environment rules.\n", encoding="utf-8"
        )
        _, record = load_setup(self.data, "demo")
        updated = review_setup_edits(
            self.directory,
            record,
            offline=False,
            review_command_builder=output_builder(
                "review.public.json",
                {"schema_version": 1, "summary": "No findings.", "findings": []},
            ),
        )
        self.assertNotEqual(updated["acceptance_token"], readiness["acceptance_token"])
        change = json.loads(
            next(
                (self.directory / "setup" / "edits" / "attempts").glob(
                    "*/change.public.json"
                )
            ).read_text()
        )
        self.assertEqual(change["staging_trees"], ["environment"])
        self.assertEqual(change["stages"], ["setup review"])

    def test_subject_edit_reruns_protocol_public_checks_and_review(self) -> None:
        readiness = self.ready_setup()
        subject = Path(readiness["staging"]["subject"])
        (subject / "solution" / "policy.py").write_text("VALUE = 2\n", encoding="utf-8")
        _, record = load_setup(self.data, "demo")
        review_setup_edits(
            self.directory,
            record,
            offline=False,
            review_command_builder=output_builder(
                "review.public.json",
                {"schema_version": 1, "summary": "No findings.", "findings": []},
            ),
        )
        change = json.loads(
            next(
                (self.directory / "setup" / "edits" / "attempts").glob(
                    "*/change.public.json"
                )
            ).read_text()
        )
        self.assertEqual(change["staging_trees"], ["subject"])
        self.assertEqual(
            change["stages"],
            ["protocol preflight", "public checks", "setup review"],
        )

    def test_invalid_controller_edit_is_recorded_without_a_token(self) -> None:
        readiness = self.ready_setup()
        subject = Path(readiness["staging"]["subject"])
        (subject / "_arctl" / "subject.py").write_text("raise SystemExit(0)\n")
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(StateError, "controller-owned setup file"):
            review_setup_edits(
                self.directory,
                record,
                offline=False,
                review_command_builder=output_builder(
                    "review.public.json",
                    {"schema_version": 1, "summary": "No findings.", "findings": []},
                ),
            )
        _, record = load_setup(self.data, "demo")
        self.assertEqual(record["state"], "EDIT_REVIEW_FAILED")
        self.assertNotIn("acceptance_token", record)
        change = next(
            (self.directory / "setup" / "edits" / "attempts").glob(
                "*/change.public.json"
            )
        )
        self.assertTrue(change.is_file())

    def test_setup_commands_are_managed_and_sandbox_wrapped(self) -> None:
        root = self.directory / "setup" / "managed-test"
        command = (sys.executable, "-c", "print('ok')")
        with patch("arctl.setup.sandbox_command", side_effect=lambda wrapped, **_: wrapped) as wrapped:
            _run_setup_command(
                command,
                cwd=self.subject,
                root=root,
                read_paths=(self.subject,),
                write_paths=(root,),
                timeout_seconds=10,
                max_output_bytes=1024,
                label="test command",
            )
        wrapped.assert_called_once()
        self.assertTrue((root / "process" / "started.json").is_file())
        self.assertTrue((root / "process" / "result.json").is_file())
        self.assertEqual((root / "process" / "stdout.bin").read_text(), "ok\n")

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
        invalid = self.build_value()
        invalid["task"]["editable_paths"].append("_arctl/**")
        invalid["evaluator"]["calibration"]["ladder"] = [16, 8]
        values = [invalid, self.build_value()]
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
        self.assertIn("invalid_output_path", prompts[1])
        self.assertNotIn('"invalid_output":', prompts[1])
        self.assertLess(len(prompts[1].encode()), MAX_AGENT_PROMPT_BYTES)
        stages = [event["stage"] for event in events]
        self.assertLess(stages.index("contract repair"), stages.index("dependencies"))
        self.assertTrue(stale.exists())

    def test_invalid_contract_after_repair_reports_exact_error(self) -> None:
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", self.discovery()),
        )
        _, record = load_setup(self.data, "demo")
        save_answers(self.directory, record, {})
        invalid = self.build_value()
        invalid["task"]["editable_paths"].append("_arctl/**")
        invalid["evaluator"]["calibration"]["ladder"] = [16, 8]
        events: list[dict] = []
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(
            StateError,
            "invalid after one repair: initial validation: task contract: editable paths.*"
            "evaluator manifest contract: calibration.ladder.*repair: task contract:.*"
            "evaluator manifest contract:",
        ):
            build_setup(
                self.directory,
                record,
                offline=False,
                command_builder=output_builder("build.public.json", invalid),
                progress=lambda event: events.append(dict(event)),
            )
        self.assertNotIn("dependencies", [event["stage"] for event in events])
        _, record = load_setup(self.data, "demo")
        self.assertNotIn("pending_build", record)
        self.assertEqual(len(record["prior_build_findings"]), 2)
        prompts: list[str] = []

        def repair_builder(worktree: Path, scratch: Path, schema: Path, prompt: str):
            prompts.append(prompt)
            return output_builder("build.public.json", self.build_value())(
                worktree, scratch, schema, prompt
            )

        readiness = build_setup(
            self.directory,
            record,
            offline=False,
            command_builder=repair_builder,
            review_command_builder=output_builder(
                "review.public.json",
                {"schema_version": 1, "summary": "No findings.", "findings": []},
            ),
        )
        self.assertEqual(readiness["review"], "ready")
        self.assertEqual(len(prompts), 1)
        self.assertTrue(prompts[0].startswith("Create a minimal faithful"))
        self.assertIn("prior_build_findings", prompts[0])

    def test_build_output_schema_is_strict_at_every_object(self) -> None:
        def check(value: object) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object" or "properties" in value:
                    self.assertFalse(value.get("additionalProperties"))
                    self.assertEqual(
                        set(value.get("required", [])), set(value.get("properties", {}))
                    )
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(build_schema())

    def test_build_output_schema_gives_every_array_an_item_schema(self) -> None:
        def check(value: object) -> None:
            if isinstance(value, dict):
                if value.get("type") == "array":
                    self.assertIn("items", value)
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(build_schema())

    def test_setup_agent_error_extracts_codex_api_message_from_stdout(self) -> None:
        nested = json.dumps(
            {"error": {"message": "array schema missing items"}}
        )
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started"}),
                json.dumps({"type": "error", "message": nested}),
            ]
        )
        self.assertEqual(
            _agent_failure_detail("", stdout), "array schema missing items"
        )

    def test_typed_contract_rejects_root_prefixes_and_paired_booleans_together(self) -> None:
        value = self.build_value()
        value["environment_files"] = [
            {"path": "environment/adapter.py", "content": "pass\n"}
        ]
        value["evaluator"]["public"]["telemetry"] = [
            {
                "name": "completed",
                "description": "Whether execution completed.",
                "unit": "boolean",
                "scope": "paired",
                "role": "safety",
                "value_type": "boolean",
                "direction": "contextual",
            }
        ]
        with self.assertRaisesRegex(
            Exception, "paired metrics must be numeric.*build response contract"
        ):
            _validate_build_contract(value, self.record)

    def test_build_contract_reports_all_generated_telemetry_defects(self) -> None:
        value = self.build_value()
        value["evaluator"]["public"]["telemetry"] = [
            {
                "name": "mapped_seed_collision",
                "description": "Whether mapped seeds collided.",
                "unit": "boolean",
                "scope": "paired",
                "role": "safety",
                "value_type": "boolean",
                "direction": "lower",
            },
            {
                "name": "advisory_process_wall_time_seconds",
                "description": "Unavailable process timing.",
                "unit": "seconds",
                "scope": "comparison",
                "role": "implementation",
                "value_type": "number",
                "direction": "contextual",
            },
            {
                "name": "advisory_peak_rss_bytes",
                "description": "Unavailable process memory.",
                "unit": "bytes",
                "scope": "comparison",
                "role": "implementation",
                "value_type": "number",
                "direction": "contextual",
            },
        ]
        findings = _build_semantic_findings(value)
        self.assertTrue(any("paired metrics must be numeric" in item for item in findings))
        self.assertTrue(any("boolean metrics must be contextual" in item for item in findings))
        self.assertEqual(
            sum("process resource telemetry is unsupported" in item for item in findings),
            2,
        )

    def test_build_v3_upgrade_preserves_program_but_controller_owns_python(self) -> None:
        value = self.build_value()
        value["schema_version"] = 3
        value["task"]["public_checks"] = [["python", "-m", "unittest", "-q"]]
        value["task"]["public_probe"] = {
            "command": ["python", "tools/probe.py", "--json"],
            "trial_equivalents": 1,
        }
        upgraded = _upgrade_build_v3(value)
        self.assertEqual(upgraded["schema_version"], 4)
        self.assertEqual(
            upgraded["task"]["public_checks"][0],
            {"kind": "module", "target": "unittest", "arguments": ["-q"]},
        )
        task, _, _ = _validate_build_contract(upgraded, self.record)
        expected_python = str(self.workspace / ".venv" / "bin" / "python")
        self.assertEqual(task["public_checks"][0][0], expected_python)
        self.assertEqual(task["public_probe"]["command"][0], expected_python)

    def test_protocol_preflight_failure_forces_fresh_generation(self) -> None:
        discover_setup(
            self.directory,
            self.record,
            command_builder=output_builder("discovery.public.json", self.discovery()),
        )
        _, record = load_setup(self.data, "demo")
        save_answers(self.directory, record, {})
        broken = self.build_value()
        broken["evaluator_hook"] = broken["evaluator_hook"].replace(
            "'private_scoring': {}", "'wrong': {}"
        )
        _, record = load_setup(self.data, "demo")
        with self.assertRaisesRegex(StateError, "protocol prepare failed"):
            build_setup(
                self.directory,
                record,
                offline=False,
                command_builder=output_builder("build.public.json", broken),
            )
        _, record = load_setup(self.data, "demo")
        self.assertNotIn("pending_build", record)
        self.assertIn("protocol preflight", record["prior_build_findings"][0])

    def test_capability_downgrades_require_one_constraints_confirmation(self) -> None:
        discovery = self.discovery()
        discovery["capability_downgrades"] = [
            {"capability_id": "memory_limit"}
        ]
        with self.assertRaisesRegex(StateError, "capability downgrades"):
            discover_setup(
                self.directory,
                self.record,
                command_builder=output_builder("discovery.public.json", discovery),
            )
